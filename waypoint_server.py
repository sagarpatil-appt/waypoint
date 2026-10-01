import json
import os
import re
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts import base
from pydantic import Field

load_dotenv()

ENV_PATH = Path(__file__).resolve().parent / ".env"

try:
    _VERSION = version("waypoint")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"

# Defaults to this project's actual published home; override via env var for a fork.
_GITHUB_REPO = os.getenv("WAYPOINT_GITHUB_REPO", "sagarpatil-appt/waypoint")

_config = {
    "site_url": os.getenv("JIRA_SITE_URL", "").rstrip("/"),
    "email": os.getenv("JIRA_EMAIL", ""),
    "api_token": os.getenv("JIRA_API_TOKEN", ""),
}

_working_issue = {"key": None, "started_at": None}

# Maps a Jira project key -> {"repo_path": ..., "subdirectory": ...}, so implement_ticket can
# deterministically verify the workspace instead of guessing from the git remote every time.
_project_repos: dict = {}
try:
    _project_repos = json.loads(os.getenv("JIRA_PROJECT_REPOS", "") or "{}")
except json.JSONDecodeError:
    _project_repos = {}

_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{24}$|^\d+:[0-9a-fA-F-]{36}$")

mcp = MCPServer(
    "Waypoint",
    version=_VERSION,
    log_level="ERROR",
    instructions=(
        "Exposes Jira Cloud ticket operations. Before calling any tool other than "
        "'jira_connection_status' or 'setup_jira_connection', check whether a connection is "
        "already configured by calling 'jira_connection_status'. If it reports connected=false, "
        "ask the user for their Jira site URL, Atlassian account email, and an API token "
        "(they can create one at https://id.atlassian.com/manage-profile/security/api-tokens), "
        "then call 'setup_jira_connection' with those values before proceeding with what the "
        "user actually asked for."
    ),
)


def _is_configured() -> bool:
    return all(_config.values())


def _save_env() -> None:
    """Persist the current Jira connection to .env, preserving unrelated lines."""
    managed_keys = ("JIRA_SITE_URL=", "JIRA_EMAIL=", "JIRA_API_TOKEN=", "JIRA_PROJECT_REPOS=")
    lines = []
    if ENV_PATH.exists():
        lines = [
            line
            for line in ENV_PATH.read_text().splitlines()
            if not line.startswith(managed_keys)
        ]
    lines += [
        f"JIRA_SITE_URL={_config['site_url']}",
        f"JIRA_EMAIL={_config['email']}",
        f"JIRA_API_TOKEN={_config['api_token']}",
        f"JIRA_PROJECT_REPOS={json.dumps(_project_repos)}",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)


def _client() -> httpx.AsyncClient:
    if not _is_configured():
        raise ValueError(
            "Jira connection is not set up yet. Use the 'setup_jira_connection' tool "
            "with your Jira site URL, email, and API token first."
        )
    return httpx.AsyncClient(
        base_url=f"{_config['site_url']}/rest/api/3",
        auth=(_config["email"], _config["api_token"]),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30.0,
    )


def _adf_from_text(text: str) -> dict:
    """Wrap plain text into the Atlassian Document Format the v3 API requires."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}] if text else [],
            }
        ],
    }


def _text_from_adf(adf) -> str:
    """Best-effort plain-text extraction from an ADF description/comment body.

    Each top-level block (paragraph, heading, list item, ...) becomes its own
    line, since ADF nests text nodes without any inherent whitespace between
    sibling blocks.
    """
    if not adf:
        return ""
    if isinstance(adf, str):
        return adf

    def extract(node) -> str:
        if not isinstance(node, dict):
            return ""
        node_type = node.get("type")
        attrs = node.get("attrs", {}) or {}

        if node_type == "text":
            return node.get("text", "")
        if node_type == "mention":
            return attrs.get("text") or f"@{attrs.get('id', 'someone')}"
        if node_type == "emoji":
            return attrs.get("text") or attrs.get("shortName", "")
        if node_type == "hardBreak":
            return "\n"
        if node_type in ("media", "mediaSingle", "mediaGroup"):
            return "[attachment]"
        if node_type == "inlineCard" or node_type == "blockCard":
            return attrs.get("url", "[link]")

        children = node.get("content", []) or []
        text = "".join(extract(child) for child in children)
        if not text and node_type not in (None, "doc", "paragraph"):
            return f"[{node_type}]"
        return text

    blocks = adf.get("content", []) if isinstance(adf, dict) else []
    return "\n".join(line for line in (extract(block) for block in blocks) if line)


def _looks_like_account_id(value: str) -> bool:
    """Heuristic for Atlassian cloud accountIds, e.g. '5b10a2844c20165700ede21g' or the
    older '712020:xxxxxxxx-xxxx-...' form, so callers who already have one can skip
    /user/search entirely (that endpoint returns nothing on sites with GDPR/privacy-mode
    user search restricted, which is common on enterprise Jira sites)."""
    return bool(_ACCOUNT_ID_RE.match(value.strip()))


async def _resolve_account_id(client: httpx.AsyncClient, who: str) -> dict:
    """Resolve an accountId, email, or display name to {"account_id", "display_name"}.

    Tries the value as a literal accountId first (works regardless of site privacy
    settings), then falls back to /user/search (email/display name lookup, which some
    Jira sites restrict under GDPR/privacy mode and will return no results for)."""
    who = who.strip()
    if _looks_like_account_id(who):
        response = await client.get("/user", params={"accountId": who})
        await _raise_for_status(response)
        user = response.json()
        return {"account_id": user["accountId"], "display_name": user.get("displayName")}

    search = await client.get("/user/search", params={"query": who, "maxResults": 1})
    await _raise_for_status(search)
    users = search.json()
    if not users:
        raise ValueError(
            f"No Jira user found matching '{who}'. If this site restricts user search "
            "(GDPR/privacy mode is common on enterprise Jira), pass their Atlassian "
            "accountId directly instead of an email or display name."
        )
    return {"account_id": users[0]["accountId"], "display_name": users[0].get("displayName")}


async def _fetch_time_tracking_config(client: httpx.AsyncClient) -> tuple[float, float]:
    """Fetch this site's (hours_per_day, days_per_week) for converting elapsed wall-clock
    time into Jira's duration shorthand. Falls back to Jira's own defaults (8h/5d) if the
    site hasn't customized this or the endpoint is unavailable."""
    try:
        response = await client.get("/configuration")
        await _raise_for_status(response)
        tracking = response.json().get("timeTrackingConfiguration", {})
        return (
            float(tracking.get("workingHoursPerDay", 8)),
            float(tracking.get("workingDaysPerWeek", 5)),
        )
    except (ValueError, KeyError):
        return 8.0, 5.0


def _format_duration_jira(seconds: float, hours_per_day: float, days_per_week: float) -> str:
    """Format elapsed seconds as Jira duration shorthand (e.g. '1h 30m'), using this site's
    actual working-hours/working-days configuration rather than assuming calendar time."""
    minutes = max(1, round(seconds / 60))
    week_minutes = round(hours_per_day * days_per_week * 60)
    day_minutes = round(hours_per_day * 60)

    weeks, minutes = divmod(minutes, week_minutes) if week_minutes else (0, minutes)
    days, minutes = divmod(minutes, day_minutes) if day_minutes else (0, minutes)
    hours, minutes = divmod(minutes, 60)

    parts = []
    if weeks:
        parts.append(f"{weeks}w")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


async def _raise_for_status(response: httpx.Response):
    if response.status_code >= 400:
        raise ValueError(
            f"Jira API error {response.status_code}: {response.text[:500]}"
        )


async def _fetch_all_comments(client: httpx.AsyncClient, issue_key: str) -> list:
    """Fetch every comment on an issue via the dedicated paginated endpoint.

    The comment array embedded in a GET /issue response (via fields=*all) is capped at
    Jira's default page size, so a ticket with many comments would silently lose the rest.
    """
    comments = []
    start_at = 0
    max_results = 100
    while True:
        response = await client.get(
            f"/issue/{issue_key}/comment",
            params={"startAt": start_at, "maxResults": max_results, "orderBy": "created"},
        )
        await _raise_for_status(response)
        data = response.json()
        comments.extend(data.get("comments", []))
        start_at += max_results
        if start_at >= data.get("total", len(comments)):
            break
    return comments


@mcp.tool(
    name="setup_jira_connection",
    description=(
        "Connect this server to a Jira Cloud site for the first time (or reconnect to a "
        "different one). Validates the site URL, email, and API token against Jira before "
        "saving them, and persists them to .env so future sessions don't need to reconnect. "
        "Run this first if 'jira_connection_status' reports not connected."
    ),
)
async def setup_jira_connection(
    site_url: str = Field(
        description="Jira Cloud site URL, e.g. 'https://yourcompany.atlassian.net'"
    ),
    email: str = Field(description="Atlassian account email used to sign in to Jira"),
    api_token: str = Field(
        description=(
            "Atlassian API token for that account (create one at "
            "https://id.atlassian.com/manage-profile/security/api-tokens)"
        )
    ),
):
    site_url = site_url.strip().rstrip("/")
    if not site_url.startswith("http"):
        site_url = f"https://{site_url}"
    email = email.strip()
    api_token = api_token.strip()

    async with httpx.AsyncClient(
        base_url=f"{site_url}/rest/api/3",
        auth=(email, api_token),
        headers={"Accept": "application/json"},
        timeout=30.0,
    ) as client:
        response = await client.get("/myself")

    if response.status_code == 401:
        raise ValueError(
            "Jira rejected these credentials (401 Unauthorized). Double-check the email "
            "and API token and try again."
        )
    await _raise_for_status(response)
    profile = response.json()

    _config["site_url"] = site_url
    _config["email"] = email
    _config["api_token"] = api_token
    _save_env()

    return {
        "status": "connected",
        "site_url": site_url,
        "account": profile.get("displayName"),
    }


@mcp.tool(
    name="jira_connection_status",
    description="Check whether this server is currently connected to a Jira site.",
)
async def jira_connection_status():
    if not _is_configured():
        return {"connected": False}
    return {
        "connected": True,
        "site_url": _config["site_url"],
        "email": _config["email"],
    }


@mcp.tool(
    name="check_for_updates",
    description=(
        "Check whether a newer version of Waypoint is available. Safe to call any time; "
        "worth checking occasionally since this server has no other way to notify you of "
        "updates (it's a local stdio process, not a background service)."
    ),
)
async def check_for_updates():
    if not _GITHUB_REPO:
        return {
            "current_version": _VERSION,
            "update_available": None,
            "note": "No GitHub repo configured yet (WAYPOINT_GITHUB_REPO unset) — can't check.",
        }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
        except httpx.HTTPError as e:
            return {
                "current_version": _VERSION,
                "update_available": None,
                "note": f"Could not reach GitHub to check for updates: {e}",
            }

    if response.status_code == 404:
        return {
            "current_version": _VERSION,
            "update_available": None,
            "note": f"No releases published yet at github.com/{_GITHUB_REPO}.",
        }
    await _raise_for_status(response)
    latest = response.json()
    latest_version = (latest.get("tag_name") or "").lstrip("v")

    return {
        "current_version": _VERSION,
        "latest_version": latest_version or None,
        "update_available": bool(latest_version) and latest_version != _VERSION,
        "release_url": latest.get("html_url"),
        "release_notes": latest.get("body"),
    }


def _issue_summary(issue: dict) -> dict:
    fields = issue["fields"]
    return {
        "key": issue["key"],
        "url": f"{_config['site_url']}/browse/{issue['key']}",
        "summary": fields["summary"],
        "status": fields["status"]["name"],
        "issue_type": fields["issuetype"]["name"],
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "assignee_account_id": (fields.get("assignee") or {}).get("accountId"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "reporter_account_id": (fields.get("reporter") or {}).get("accountId"),
        "priority": (fields.get("priority") or {}).get("name"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
    }


@mcp.tool(
    name="search_tickets",
    description=(
        "Search Jira tickets using JQL (Jira Query Language) and return a summary of matching "
        "issues, along with the total match count so a capped result set (more matches than "
        "max_results) is visible rather than silently looking complete."
    ),
)
async def search_tickets(
    jql: str = Field(
        description='JQL query, e.g. \'project = "ABC" AND status = "To Do"\''
    ),
    max_results: int = Field(default=20, description="Maximum number of issues to return"),
):
    async with _client() as client:
        response = await client.get(
            "/search/jql",
            params={
                "jql": jql,
                "maxResults": max_results,
                "fields": "summary,status,issuetype,assignee,reporter,priority,created,updated",
            },
        )
        await _raise_for_status(response)
        data = response.json()

    issues = [_issue_summary(issue) for issue in data.get("issues", [])]
    return {
        "total": data.get("total", len(issues)),
        "returned": len(issues),
        "issues": issues,
    }


@mcp.tool(
    name="my_open_tickets",
    description="List the current user's open (not Done) Jira tickets, most recently updated first.",
)
async def my_open_tickets(
    max_results: int = Field(default=20, description="Maximum number of issues to return"),
):
    async with _client() as client:
        response = await client.get(
            "/search/jql",
            params={
                "jql": "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
                "maxResults": max_results,
                "fields": "summary,status,issuetype,assignee,reporter,priority,created,updated",
            },
        )
        await _raise_for_status(response)
        data = response.json()

    issues = [_issue_summary(issue) for issue in data.get("issues", [])]
    return {
        "total": data.get("total", len(issues)),
        "returned": len(issues),
        "issues": issues,
    }


@mcp.tool(
    name="get_ticket",
    description="Read the full details of a single Jira ticket by its key (e.g. 'ABC-123'), including its attachments and comments.",
)
async def get_ticket(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
):
    async with _client() as client:
        response = await client.get(f"/issue/{issue_key}", params={"fields": "*all"})
        await _raise_for_status(response)
        data = response.json()
        all_comments = await _fetch_all_comments(client, issue_key)

    fields = data["fields"]
    return {
        "key": data["key"],
        "url": f"{_config['site_url']}/browse/{data['key']}",
        "summary": fields["summary"],
        "description": _text_from_adf(fields.get("description")),
        "status": fields["status"]["name"],
        "issue_type": fields["issuetype"]["name"],
        "project": fields["project"]["key"],
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "assignee_account_id": (fields.get("assignee") or {}).get("accountId"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "reporter_account_id": (fields.get("reporter") or {}).get("accountId"),
        "priority": (fields.get("priority") or {}).get("name"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "attachments": [
            {
                "filename": attachment["filename"],
                "size": attachment["size"],
                "mime_type": attachment["mimeType"],
                "url": attachment["content"],
                "author": (attachment.get("author") or {}).get("displayName"),
                "created": attachment.get("created"),
            }
            for attachment in fields.get("attachment", []) or []
        ],
        "comments": [
            {
                "author": (comment.get("author") or {}).get("displayName"),
                "author_account_id": (comment.get("author") or {}).get("accountId"),
                "author_email": (comment.get("author") or {}).get("emailAddress"),
                "created": comment.get("created"),
                "updated": comment.get("updated"),
                "body": _text_from_adf(comment.get("body")),
                "body_adf": comment.get("body"),
            }
            for comment in all_comments
        ],
    }


async def _fetch_project_issue_types(client: httpx.AsyncClient, project_key: str) -> list:
    response = await client.get(f"/project/{project_key}")
    await _raise_for_status(response)
    return response.json().get("issueTypes", [])


@mcp.tool(
    name="list_issue_types",
    description=(
        "List every issue type available in a Jira project — including whether each one is a "
        "sub-task type. Check this before create_ticket or create_subtask whenever the exact "
        "type name isn't already known: projects are configured differently (some have "
        "multiple sub-task types like 'Dev' vs 'Story Bug', or non-obvious main-type names), "
        "so guessing risks picking the wrong one instead of asking which one applies."
    ),
)
async def list_issue_types(
    project_key: str = Field(description="Jira project key, e.g. 'ABC'"),
):
    async with _client() as client:
        issue_types = await _fetch_project_issue_types(client, project_key)

    return [
        {"name": t["name"], "id": t["id"], "subtask": bool(t.get("subtask"))}
        for t in issue_types
    ]


@mcp.tool(
    name="create_ticket",
    description=(
        "Create a new Jira ticket in a project. issue_type defaults to 'Task', but if that's "
        "not clearly right for this project, call list_issue_types first and pass the exact "
        "name rather than assuming — this raises a clear error listing the valid names if the "
        "given one doesn't match, instead of leaving the choice to guesswork."
    ),
)
async def create_ticket(
    project_key: str = Field(description="Project key the ticket belongs to, e.g. 'ABC'"),
    summary: str = Field(description="Short title of the ticket"),
    description: str = Field(default="", description="Longer description of the ticket"),
    issue_type: str = Field(default="Task", description="Issue type name, e.g. 'Task', 'Bug', 'Story'"),
    priority: str = Field(default="", description="Priority name, e.g. 'High', 'Medium', 'Low'; leave empty for the project default"),
    labels: list[str] = Field(default_factory=list, description="Labels to apply to the ticket"),
    components: list[str] = Field(default_factory=list, description="Component names to apply to the ticket"),
):
    async with _client() as client:
        issue_types = await _fetch_project_issue_types(client, project_key)
        main_types = [t for t in issue_types if not t.get("subtask")]
        match = next(
            (t for t in main_types if t["name"].lower() == issue_type.strip().lower()), None
        )
        if not match:
            available = ", ".join(t["name"] for t in main_types) or "none found"
            raise ValueError(
                f"'{issue_type}' is not a valid issue type for project {project_key}. "
                f"Available: {available}"
            )

        fields = {
            "project": {"key": project_key},
            "summary": summary,
            "description": _adf_from_text(description),
            "issuetype": {"name": match["name"]},
        }
        if priority:
            fields["priority"] = {"name": priority}
        if labels:
            fields["labels"] = labels
        if components:
            fields["components"] = [{"name": c} for c in components]

        payload = {"fields": fields}
        response = await client.post("/issue", json=payload)
        await _raise_for_status(response)
        data = response.json()

    return {"key": data["key"], "url": f"{_config['site_url']}/browse/{data['key']}"}


@mcp.tool(
    name="create_subtask",
    description=(
        "Create a sub-task under an existing Jira ticket. If the project has more than one "
        "sub-task issue type, issue_type is required — call list_issue_types first to see "
        "the options; this tool refuses to guess between them rather than silently picking "
        "one (e.g. 'Story Bug' instead of the intended 'Dev')."
    ),
)
async def create_subtask(
    parent_key: str = Field(description="Key of the parent ticket, e.g. 'ABC-123'"),
    summary: str = Field(description="Short title of the sub-task"),
    description: str = Field(default="", description="Longer description of the sub-task"),
    issue_type: str = Field(
        default="",
        description=(
            "Sub-task issue type name, e.g. 'Dev', 'Story Bug'. Required if the project has "
            "more than one sub-task type; call list_issue_types to see the options."
        ),
    ),
):
    async with _client() as client:
        parent = await client.get(f"/issue/{parent_key}", params={"fields": "project"})
        await _raise_for_status(parent)
        project_key = parent.json()["fields"]["project"]["key"]

        issue_types = await _fetch_project_issue_types(client, project_key)
        subtask_types = [t for t in issue_types if t.get("subtask")]

        if issue_type.strip():
            match = next(
                (t for t in subtask_types if t["name"].lower() == issue_type.strip().lower()),
                None,
            )
            if not match:
                available = ", ".join(t["name"] for t in subtask_types) or "none found"
                raise ValueError(
                    f"'{issue_type}' is not a valid sub-task type for project {project_key}. "
                    f"Available: {available}"
                )
            subtask_type_name = match["name"]
        elif len(subtask_types) > 1:
            available = ", ".join(t["name"] for t in subtask_types)
            raise ValueError(
                f"Project {project_key} has multiple sub-task types ({available}) — pass "
                "issue_type explicitly rather than guessing which one is intended."
            )
        else:
            subtask_type_name = subtask_types[0]["name"] if subtask_types else "Subtask"

        payload = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "summary": summary,
                "description": _adf_from_text(description),
                "issuetype": {"name": subtask_type_name},
            }
        }
        response = await client.post("/issue", json=payload)
        await _raise_for_status(response)
        data = response.json()

    return {"key": data["key"], "url": f"{_config['site_url']}/browse/{data['key']}"}


@mcp.tool(
    name="add_comment",
    description=(
        "Add a comment to an existing Jira ticket. Write it the way a developer would type a "
        "quick note themselves — plain, natural language, not formal or robotic phrasing. If "
        "there's a relevant screenshot or file (shared by the user or produced while "
        "investigating), also call add_attachment so it's actually visible on the ticket "
        "instead of only described in text."
    ),
)
async def add_comment(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    comment: str = Field(description="Comment text to add"),
):
    payload = {"body": _adf_from_text(comment)}
    async with _client() as client:
        response = await client.post(f"/issue/{issue_key}/comment", json=payload)
        await _raise_for_status(response)

    return {"status": "comment added", "issue_key": issue_key}


@mcp.tool(
    name="get_available_transitions",
    description=(
        "List the status transitions currently available for a ticket. Check this before "
        "calling update_ticket_status if the exact status name isn't already known — status "
        "names are workflow-specific (e.g. a project may use 'Started' instead of "
        "'In Progress')."
    ),
)
async def get_available_transitions(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
):
    async with _client() as client:
        response = await client.get(f"/issue/{issue_key}/transitions")
        await _raise_for_status(response)
        transitions = response.json()["transitions"]

    return [{"name": t["name"], "to_status": t["to"]["name"]} for t in transitions]


@mcp.tool(
    name="update_ticket_status",
    description=(
        "Change a Jira ticket's status by transitioning it to the given status name "
        "(e.g. 'In Progress', 'Done', 'To Do'). If unsure of the exact name, call "
        "get_available_transitions first."
    ),
)
async def update_ticket_status(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    status: str = Field(description="Target status name, e.g. 'In Progress', 'Done'"),
):
    async with _client() as client:
        transitions_response = await client.get(f"/issue/{issue_key}/transitions")
        await _raise_for_status(transitions_response)
        transitions = transitions_response.json()["transitions"]

        match = next(
            (t for t in transitions if t["name"].lower() == status.strip().lower()), None
        )
        if not match:
            available = ", ".join(t["name"] for t in transitions)
            raise ValueError(
                f"'{status}' is not a valid transition for {issue_key}. "
                f"Available transitions: {available}"
            )

        response = await client.post(
            f"/issue/{issue_key}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        await _raise_for_status(response)

    return {"issue_key": issue_key, "status": match["to"]["name"]}


@mcp.tool(
    name="update_ticket_assignee",
    description=(
        "Change who a Jira ticket is assigned to. Pass an email, display name, or Atlassian "
        "accountId to look up (accountId works even on sites that restrict user search under "
        "GDPR/privacy mode); leave assignee empty to unassign the ticket."
    ),
)
async def update_ticket_assignee(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    assignee: str = Field(
        default="",
        description="Email, display name, or accountId of the new assignee; empty to unassign",
    ),
):
    async with _client() as client:
        if not assignee.strip():
            payload = {"accountId": None}
            display_name = None
        else:
            resolved = await _resolve_account_id(client, assignee)
            payload = {"accountId": resolved["account_id"]}
            display_name = resolved["display_name"]

        response = await client.put(f"/issue/{issue_key}/assignee", json=payload)
        await _raise_for_status(response)

    return {"issue_key": issue_key, "assignee": display_name}


@mcp.tool(
    name="add_worklog",
    description=(
        "Log time spent working on a Jira ticket. Leave time_spent empty to auto-compute it "
        "from the real elapsed wall-clock time since set_working_issue was called for this "
        "ticket (converted using this site's actual working-hours/working-days config) rather "
        "than guessing a duration. That elapsed time is calendar time, not verified focus "
        "time — if it looks implausible (spans a lunch break, meetings, or multiple days), "
        "pass an explicit time_spent instead of trusting it blindly."
    ),
)
async def add_worklog(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    time_spent: str = Field(
        default="",
        description=(
            "Time spent in Jira shorthand, e.g. '1h 30m', '2d', '45m'; leave empty to "
            "auto-compute from elapsed time since set_working_issue was called"
        ),
    ),
    comment: str = Field(default="", description="Optional note describing the work done"),
):
    async with _client() as client:
        if not time_spent.strip():
            if _working_issue["key"] != issue_key or not _working_issue["started_at"]:
                raise ValueError(
                    "No time_spent given and no active working-issue timer for "
                    f"{issue_key}. Call set_working_issue first, or pass time_spent explicitly."
                )
            elapsed_seconds = (
                datetime.now(timezone.utc) - _working_issue["started_at"]
            ).total_seconds()
            hours_per_day, days_per_week = await _fetch_time_tracking_config(client)
            time_spent = _format_duration_jira(elapsed_seconds, hours_per_day, days_per_week)

        payload = {"timeSpent": time_spent}
        if comment:
            payload["comment"] = _adf_from_text(comment)

        response = await client.post(f"/issue/{issue_key}/worklog", json=payload)
        await _raise_for_status(response)
        data = response.json()

    return {
        "issue_key": issue_key,
        "worklog_id": data["id"],
        "time_spent": data["timeSpent"],
    }


@mcp.tool(
    name="list_projects",
    description="List Jira projects visible to the current user, to find a valid project key before creating a ticket.",
)
async def list_projects():
    async with _client() as client:
        response = await client.get("/project/search", params={"maxResults": 100})
        await _raise_for_status(response)
        data = response.json()

    return [{"key": p["key"], "name": p["name"]} for p in data.get("values", [])]


@mcp.tool(
    name="set_project_workspace",
    description=(
        "Remember which local repo (and optional subdirectory, for monorepos) a Jira project's "
        "tickets get implemented in. Once set, implement_ticket can verify the workspace "
        "deterministically instead of guessing from the git remote every time."
    ),
)
async def set_project_workspace(
    project_key: str = Field(description="Jira project key, e.g. 'ABC'"),
    repo_path: str = Field(description="Absolute path to the local git repo for this project"),
    subdirectory: str = Field(
        default="",
        description="Optional subdirectory within the repo, for monorepos with multiple projects",
    ),
):
    path = Path(repo_path)
    if not path.is_dir():
        raise ValueError(f"Not a directory: {repo_path}")

    _project_repos[project_key.strip().upper()] = {
        "repo_path": str(path.resolve()),
        "subdirectory": subdirectory.strip(),
    }
    _save_env()
    return {"project_key": project_key.strip().upper(), **_project_repos[project_key.strip().upper()]}


@mcp.tool(
    name="get_project_workspace",
    description="Look up the local repo (and subdirectory, if any) previously set for a Jira project via set_project_workspace.",
)
async def get_project_workspace(
    project_key: str = Field(description="Jira project key, e.g. 'ABC'"),
):
    mapping = _project_repos.get(project_key.strip().upper())
    if not mapping:
        return {"project_key": project_key.strip().upper(), "known": False}
    return {"project_key": project_key.strip().upper(), "known": True, **mapping}


@mcp.tool(
    name="add_attachment",
    description="Attach a local file to a Jira ticket.",
)
async def add_attachment(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    file_path: str = Field(description="Absolute path to the local file to attach"),
):
    if not _is_configured():
        raise ValueError(
            "Jira connection is not set up yet. Use the 'setup_jira_connection' tool "
            "with your Jira site URL, email, and API token first."
        )

    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"File not found: {file_path}")

    async with httpx.AsyncClient(
        base_url=f"{_config['site_url']}/rest/api/3",
        auth=(_config["email"], _config["api_token"]),
        headers={"Accept": "application/json", "X-Atlassian-Token": "no-check"},
        timeout=60.0,
    ) as client:
        with open(path, "rb") as f:
            response = await client.post(f"/issue/{issue_key}/attachments", files={"file": (path.name, f)})
        await _raise_for_status(response)
        data = response.json()

    return [{"filename": a["filename"], "size": a["size"], "url": a["content"]} for a in data]


@mcp.tool(
    name="add_watcher",
    description=(
        "Add a watcher to a Jira ticket by email, display name, or Atlassian accountId "
        "(accountId works even on sites that restrict user search under GDPR/privacy mode)."
    ),
)
async def add_watcher(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
    watcher: str = Field(description="Email, display name, or accountId of the user to add"),
):
    async with _client() as client:
        resolved = await _resolve_account_id(client, watcher)

        response = await client.post(
            f"/issue/{issue_key}/watchers", json=resolved["account_id"]
        )
        await _raise_for_status(response)

    return {"issue_key": issue_key, "watcher": resolved["display_name"]}


@mcp.tool(
    name="list_watchers",
    description="List the watchers on a Jira ticket, including each one's accountId.",
)
async def list_watchers(
    issue_key: str = Field(description="Jira issue key, e.g. 'ABC-123'"),
):
    async with _client() as client:
        response = await client.get(f"/issue/{issue_key}/watchers")
        await _raise_for_status(response)
        data = response.json()

    return [
        {"display_name": w.get("displayName"), "account_id": w.get("accountId")}
        for w in data.get("watchers", [])
    ]


@mcp.tool(
    name="list_link_types",
    description="List the valid issue link type names for this Jira site (used by link_tickets).",
)
async def list_link_types():
    async with _client() as client:
        response = await client.get("/issueLinkType")
        await _raise_for_status(response)
        data = response.json()

    return [
        {"name": t["name"], "inward": t["inward"], "outward": t["outward"]}
        for t in data.get("issueLinkTypes", [])
    ]


@mcp.tool(
    name="link_tickets",
    description=(
        "Create a link between two Jira tickets (e.g. blocks, relates to, duplicates). "
        "Call list_link_types first if the exact link type name isn't already known."
    ),
)
async def link_tickets(
    from_key: str = Field(description="Jira issue key of the first ticket, e.g. 'ABC-123'"),
    to_key: str = Field(description="Jira issue key of the second ticket, e.g. 'ABC-456'"),
    link_type: str = Field(default="Relates", description="Link type name, e.g. 'Blocks', 'Relates', 'Duplicate'"),
):
    payload = {
        "type": {"name": link_type},
        "inwardIssue": {"key": from_key},
        "outwardIssue": {"key": to_key},
    }
    async with _client() as client:
        response = await client.post("/issueLink", json=payload)
        await _raise_for_status(response)

    return {"status": "linked", "from": from_key, "to": to_key, "link_type": link_type}


@mcp.tool(
    name="list_favorite_filters",
    description=(
        "List the current user's favourite (saved) Jira filters, including each filter's JQL "
        "so it can be run via search_tickets."
    ),
)
async def list_favorite_filters():
    async with _client() as client:
        response = await client.get("/filter/favourite")
        await _raise_for_status(response)
        data = response.json()

    return [{"id": f["id"], "name": f["name"], "jql": f["jql"]} for f in data]


@mcp.tool(
    name="set_working_issue",
    description=(
        "Mark a Jira ticket as the current working issue for this session, so its key doesn't "
        "need to be repeated in every subsequent request. Also starts this ticket's elapsed-time "
        "timer, which add_worklog can use to auto-log real time spent instead of a guess."
    ),
)
async def set_working_issue(
    issue_key: str = Field(description="Jira issue key to set as the working issue, e.g. 'ABC-123'"),
):
    async with _client() as client:
        response = await client.get(f"/issue/{issue_key}", params={"fields": "summary"})
        await _raise_for_status(response)
        summary = response.json()["fields"]["summary"]

    _working_issue["key"] = issue_key
    _working_issue["started_at"] = datetime.now(timezone.utc)
    return {"working_issue": issue_key, "summary": summary}


@mcp.tool(
    name="get_working_issue",
    description="Get the Jira ticket currently marked as the working issue for this session, if any.",
)
async def get_working_issue():
    return {"working_issue": _working_issue["key"]}


@mcp.prompt(
    name="tour",
    description=(
        "A quick guided tour of Waypoint for a new user: checks the connection, explains what "
        "this server does, and walks through the main workflow with real examples from your "
        "own Jira site. Good first thing to run after installing."
    ),
)
def tour() -> list[base.Message]:
    prompt = """
    Give me a quick guided tour of this MCP server (Waypoint) as if I just installed it.

    Steps to follow:
    1. Call jira_connection_status. If not connected, walk me through setup_jira_connection
       (ask for site URL, email, API token) before continuing.
    2. Call check_for_updates and mention the result in passing (don't dwell on it if there's
       nothing to report).
    3. In a few sentences, explain what this server is for: it routes a ticket through to
       shipped code for a single developer working one ticket at a time — not a general Jira
       administration tool (no sprints/boards/epics).
    4. Call my_open_tickets and show me what's actually on my plate right now, using real data
       instead of a hypothetical example.
    5. Briefly explain the main workflow: the implement_ticket prompt reads a ticket end to
       end, verifies it's clear enough to act on (or stops and comments if not), writes the
       code, verifies it, and then walks through committing/pushing with my confirmation at
       each risky step. Mention plan_ticket too, for breaking a bigger ticket into sub-tasks
       before implementing.
    6. Suggest one concrete next action based on what you found in step 4 (e.g. "want me to
       run implement_ticket on <a real ticket key from your list>?"), rather than a generic
       "let me know if you have questions."
    """

    return [base.UserMessage(prompt)]


@mcp.prompt(
    name="plan_ticket",
    description="Read a Jira ticket, assess whether the requirement is well-formed, draft a coding plan, and create sub-tasks for it.",
)
def plan_ticket(
    issue_key: str = Field(description="Jira issue key to analyze and plan, e.g. 'ABC-123'"),
) -> list[base.Message]:
    prompt = f"""
    Analyze the Jira ticket {issue_key} and turn it into an actionable plan.

    Steps to follow:
    1. Use the get_ticket tool to read {issue_key}'s summary and description.
    2. Assess whether the requirement is clear and well-scoped enough to act on.
       If it is vague or missing key details, say so explicitly instead of guessing.
    3. If it is workable, draft a short coding plan: the concrete steps needed to
       implement it, in order.
    4. For each significant step in the plan, use the create_subtask tool to create
       a sub-task under {issue_key} with a clear summary and description. If create_subtask
       reports that this project has more than one sub-task type, call list_issue_types and
       pick the right one explicitly (e.g. 'Dev' for implementation work) — don't let it
       default to whichever type happens to come first.
    5. Summarize what you found and what sub-tasks you created.
    """

    return [base.UserMessage(prompt)]


@mcp.prompt(
    name="implement_ticket",
    description=(
        "Read a Jira ticket end to end and either flag it as unclear via a comment, or "
        "implement it: verify the workspace, analyze the code, write the change, verify it "
        "actually passes, report back, update status/worklog, then commit (scoped, on a "
        "branch matching the repo's convention) and optionally push/PR with the user's say-so."
    ),
)
def implement_ticket(
    issue_key: str = Field(description="Jira issue key to work on, e.g. 'ABC-123'"),
) -> list[base.Message]:
    prompt = f"""
    Work on the Jira ticket {issue_key} end to end.

    Steps to follow:
    1. Use the get_ticket tool to read {issue_key}'s summary, description, comments, and
       attachments in full.
    2. Verify you are in the correct workspace before changing anything:
       a. Call get_project_workspace for {issue_key}'s project. If it returns a known mapping,
          confirm the current directory is that repo_path (and subdirectory, if set); if not,
          say so and stop rather than guessing.
       b. If no mapping is known yet, fall back to checking the current repo's remote (e.g.
          `git remote -v`) or directory name against the project. If it doesn't clearly match,
          or you can't tell, ask the user to confirm this is the right repo before proceeding.
       c. Once the user has confirmed (or you're confident from the mapping), call
          set_project_workspace to save it, so future tickets in this project don't require
          asking again.
    3. Assess whether the requirement is clear and well-scoped enough to implement.
       - If it is vague, missing key details, or contradicts what you find while
         investigating the code: use the add_comment tool to post the specific gap(s) back
         on {issue_key}, explain what you found, and STOP. Do not guess and do not write code.
         Write it like a developer's quick note — plain language, not formal or robotic — and
         if there's a relevant screenshot, attach it via add_attachment too, not just text.
    4. If it is workable:
       a. Use the set_working_issue tool to mark {issue_key} as the working issue for this
          session (this also starts its elapsed-time timer).
       b. Use the update_ticket_status tool to move {issue_key} into an in-progress-style
          status (pick the closest match from whatever is actually available for this
          ticket).
       c. Analyze the existing code relevant to this ticket (read the affected files, trace
          how the current behavior works) before drafting any plan.
       d. Draft a short implementation plan, then make the code changes in the relevant
          project using your normal file/code tools, following that project's existing
          conventions rather than introducing new ones.
       e. Verify the change (run relevant tests/build/lint if the project has them).
          - If verification fails: make a reasonable attempt to fix it. If it still fails
            after that, do NOT commit or push. Use the add_comment tool to explain what's
            failing and why you're stopping, then report back to the user. Treat this the
            same as an unclear requirement — broken code does not get committed.
       f. Use the add_comment tool to post a concise summary of what changed and why back on
          {issue_key} — write it like a developer's quick note, plain language, not formal or
          robotic, and attach any relevant screenshot via add_attachment rather than only
          describing it in text.
       g. Use the add_worklog tool to log time on {issue_key} — leave time_spent empty so it
          auto-computes from the real elapsed time since step 4a, unless that elapsed duration
          looks implausible (e.g. it spans a long break, meetings, or multiple days), in which
          case ask the user for the real time to log instead of trusting it blindly.
       h. Use the update_ticket_status tool to move {issue_key} forward. Prefer a review-style
          status (e.g. "In Review") over a done/closed-style status here — the code has only
          been committed, not reviewed or merged, so jumping straight to "Done" would be
          premature. Only use a done-style status if no review-style status exists in this
          project's workflow and the user has already confirmed the change is merged/deployed.
       i. Before creating a branch or committing, check this repo's existing convention (e.g.
          `git branch -a`, `git log --oneline -20` for recently merged branch names) rather
          than inventing your own naming scheme. If no clear convention exists, default to
          `{issue_key}-<short-kebab-slug>`.
       j. Ask the user whether to commit directly on the current branch or create a new
          branch for {issue_key} first (using the convention from step i). Do not decide this
          yourself.
       k. Stage only the files this task actually touched, by name — never a blanket `git add
          -A`/`git add .`, since the working tree may already contain unrelated uncommitted
          work that shouldn't be swept into this commit.
       l. Commit those staged changes (creating the branch first if that's what the user
          chose) with a message in the form `{issue_key}: <short summary>` using your normal
          git tools.
       m. Ask the user explicitly before pushing, even if they already chose to commit
          directly — pushing is a separate, hard-to-reverse decision. Only push after they
          confirm.
       n. If a new branch was pushed, ask the user whether to open a pull/merge request. If
          yes, use whatever forge tooling is available (e.g. `gh pr create` for GitHub, `glab
          mr create` for GitLab); if no such CLI is available, give the user the compare URL
          instead of guessing at API calls. Skip this step entirely if they committed directly
          to an existing branch — there's no new branch to open a PR from.
    5. Report back to the user what you found, what you changed (with file paths), and what
       you updated on the ticket, including the commit/branch/push/PR outcome.
    """

    return [base.UserMessage(prompt)]


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
