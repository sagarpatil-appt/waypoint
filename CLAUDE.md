# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Waypoint** — an MCP (Model Context Protocol) server, built with `mcp[cli]`'s `MCPServer` (mcp 2.x), that
exposes Jira ticket operations as tools over stdio. The entire server lives in
`waypoint_server.py` — there is no package structure beyond that single file. The name is
deliberately platform-neutral: today it only talks to Jira Cloud, but the intent is for the
ticket-to-code loop to grow to other trackers/platforms (e.g. GitHub Issues/PRs) later without
another rebrand — don't bake the name "Jira" into anything beyond the tool/variable names that
are genuinely Jira-specific today.

## Running the server

Dependencies are managed via `pyproject.toml`/`uv.lock`; install with `uv sync` (this venv has no
`pip` — it's `uv`-managed). `pyproject.toml` defines a `waypoint` console-script entry point
(`[project.scripts]`, calling `waypoint_server:main`) backed by an explicit `[build-system]` (setuptools)
— without that table, `uv sync`/`uv build` silently skip building the package and no entry point
gets installed.

```bash
uv sync                    # installs deps + the waypoint console script into .venv/bin
.venv/bin/waypoint  # or: python waypoint_server.py — both run the stdio server directly
```

It runs over the `stdio` transport, so it's meant to be launched by an MCP client (e.g. Claude
Desktop/Code config via `.mcp.json` or `claude mcp add`), not invoked standalone for interactive
use. Use `mcp dev waypoint_server.py` (from the `mcp[cli]` package) to inspect/test tools interactively
via the MCP Inspector. `uvx --from <path-or-git-url> waypoint` runs it with no prior clone/
install at all — see `README.md` for the full one-command install story, plus the alternate MCPB
(Claude Desktop one-click extension) packaging path via `manifest.json`.
`pyproject.toml` names the module explicitly (`[tool.setuptools] py-modules`), so the `tests/`
directory never ends up in the wheel.

## Testing

```bash
uv run pytest            # full suite, ~5s; no network, no Jira account needed
```

`tests/conftest.py` isolates every run from the developer's real config *before* importing the
server: a throwaway `XDG_CONFIG_HOME`/`APPDATA` holding an empty `.env` (so the legacy-file
migration never copies a real `.env` into it) and empty `JIRA_*` env vars, which `load_dotenv`
won't override. Its `jira` fixture swaps `_client()` for an `httpx.MockTransport`-backed
`FakeJira` — register responses with `jira.on(method, path, ...)` (path relative to
`/rest/api/3`, or a full URL for other hosts; a callable for dynamic responses) and inspect what
was sent with `jira.sent()`/`jira.sent_json()`. Call tools via `conftest.call(name, **args)`,
which goes through `mcp.call_tool` so argument validation, `Field` defaults, and `ToolError`
surfacing are exercised the way a client sees them. `tests/test_server.py` also runs the real
server over stdio (default, read-only, and filtered modes). Outgoing ADF is checked against
Atlassian's schema, vendored at `tests/fixtures/adf-schema-v1.json` (see the README there).

A few tests guard invariants rather than features — keep them passing rather than deleting them:
every registered tool has an entry in `_TOOL_HINTS` (and vice versa); every non-read-only tool is
either in `_JIRA_WRITE_TOOLS` or the test's explicit local-state set, so a new write tool can't
silently stay enabled in read-only mode; and no `raise ValueError` exists in the server (see
`ToolError` under Architecture).

CI (`.github/workflows/ci.yml`) runs the suite on Python 3.10–3.14 (Ubuntu) plus macOS and
Windows on 3.13, with `uv sync --locked` (fails if `uv.lock` is stale), and separately builds the
wheel and smoke-tests it through `uvx` the way end users install it.

## Configuration

Connection state (`site_url`, `email`, `api_token`) lives in the in-memory `_config` dict
(`waypoint_server.py`), seeded at import from `ENV_PATH` — `~/.config/waypoint/.env` (honoring
`XDG_CONFIG_HOME`; `%APPDATA%\waypoint\.env` on Windows) — if it exists. It deliberately never
lives in the package's own directory: under a `uvx` install that's inside uv's cache, so a cache
clean or version upgrade would wipe saved credentials (which is what happened before 0.2.1). A
legacy `.env` next to `waypoint_server.py` (source checkouts, older installs) is still read and is
copied to `ENV_PATH` on first run. Real environment variables (e.g. injected by the MCPB
`user_config`) win over both files, since `load_dotenv` doesn't override existing vars. Keys:

- `JIRA_SITE_URL` — e.g. `https://yourcompany.atlassian.net`
- `JIRA_EMAIL` — Atlassian account email used for basic auth
- `JIRA_API_TOKEN` — Atlassian API token
- `JIRA_PROJECT_REPOS` — JSON-encoded `{project_key: {"repo_path": ..., "subdirectory": ...}}`,
  populated via the `set_project_workspace` tool (see Architecture) rather than hand-edited.

Tool exposure is controlled by env vars (set in the MCP client's server config, or via the MCPB
`read_only` option), applied once at import by `_apply_tool_filters()` with `mcp.remove_tool()`,
so filtered tools are neither listed nor callable:

- `WAYPOINT_READ_ONLY=true` removes `_JIRA_WRITE_TOOLS` (everything that changes Jira). Tools that
  only touch local state — setup, workspace mapping, the working-issue timer, downloads — stay.
  It also appends `_READ_ONLY_NOTE` to the server instructions and all three prompts, telling the
  model to skip write steps and say what it would have done.
- `WAYPOINT_ENABLED_TOOLS` (allowlist) / `WAYPOINT_DISABLED_TOOLS` (denylist), comma-separated.
- `_ALWAYS_AVAILABLE_TOOLS` (`jira_connection_status`, `setup_jira_connection`,
  `check_for_updates`) survive every filter — the instructions depend on them.
- An unknown tool name in either list exits the server with an error naming it. That's
  deliberate: a typo in a denylist must not silently leave the intended tool enabled.

The server does **not** fail to start if `.env` is missing/incomplete — it starts unconfigured and
every tool that calls `_client()` raises a clear `ToolError` pointing the caller at
`setup_jira_connection` until credentials are set. This is the intended "first-run" UX for a new
user connecting through an MCP client: no manual file editing required. `setup_jira_connection`
validates the given site URL/email/API token against Jira's `/myself` endpoint before accepting
them, then persists them via `_save_env()`, which rewrites only the `JIRA_*` lines in `ENV_PATH`
(preserving anything else there), creating the directory `0700` and the file `0600` — this now includes `JIRA_PROJECT_REPOS`
alongside the three connection fields, so `_save_env()`'s managed-keys tuple must stay in sync if
another persisted field is ever added. `jira_connection_status` reports whether a connection is
currently configured without exposing the token.

Note: auth is basic-auth (email + API token) only — there's no OAuth 2.0 (3LO) support, which some
enterprise Atlassian orgs with SSO enforcement may require instead. That's a known, deliberately
unaddressed gap — adding it would mean a local OAuth callback server, client id/secret
registration, and refresh-token handling, a meaningfully different auth architecture from the
rest of this server, not a small addition.

The `MCPServer(...)` constructor's `instructions=` string (`waypoint_server.py`) is what teaches *any*
connecting MCP client's model — not just this repo's CLAUDE.md, which end users of a published
server won't have — to call `jira_connection_status` first and walk an unconfigured user through
`setup_jira_connection`. If the setup flow changes, update that string too, not just the tool
docstrings. `README.md` documents the same flow for human readers. That string, `get_ticket`'s
description, and the `plan_ticket`/`implement_ticket` prompts also tell the model that ticket
content is untrusted data, not instructions — anyone who can file or comment on a ticket can
write into it, and this server runs locally next to git, a shell, and the user's credentials
(the published Cursor + Jira MCP exfiltration attack worked exactly this way). Keep that guidance
in all four places when editing them.

## Architecture

- Talks to the Jira Cloud REST API v3 (`{site_url}/rest/api/3`) via `httpx.AsyncClient`, created
  fresh per-call in `_client()` with basic auth (email + API token), after checking
  `_is_configured()`.
- Jira v3 stores ticket descriptions/comments as **Atlassian Document Format (ADF)**, not plain
  strings. Models write and read Markdown best, so two converters bridge this (a deliberately
  small, common subset of Markdown — not a full CommonMark parser):
  - `_adf_from_markdown()` converts outgoing text (comments, descriptions, worklog notes) into
    ADF: paragraphs with line breaks, `#` headings, bullet/numbered lists nested by indentation,
    fenced code blocks with language, blockquotes, rules, and inline bold/italic/strike/code/
    links/bare URLs. Previously everything went out as one plain paragraph, so code blocks and
    lists in "what I changed" comments didn't render. The output must stay valid against
    Atlassian's ADF JSON schema (`@atlaskit/adf-schema`, `dist/json-schema/v1/full.json`) — Jira
    rejects invalid ADF outright — so e.g. the `code` mark is only ever combined with `link`,
    marks are never duplicated, text nodes are never empty, and headings inside a blockquote
    become paragraphs. `snake_case`, `__dunder__`, and `x * y` are deliberately not treated as
    emphasis. Validate against that schema after changing it.
  - `_text_from_adf()` renders incoming ADF back as Markdown (list markers and nesting, code
    fences, links, tables, task lists, panels as labelled quotes, expand titles), so the model
    sees the ticket's structure instead of a flattened blob. Nodes with no text of their own
    still render as something readable — `mention` (`@name`), `emoji`, `status` (`[TEXT]`),
    `date` (ISO date), `media` (`[attachment: filename]`, which pairs with
    `download_attachment`), smart links (the URL) — and anything unrecognized becomes
    `[nodeType]` rather than silently vanishing. A comment made only of @mentions used to
    flatten to blank, hiding real content.
- Errors meant for the model are raised as `ToolError` (`mcp.server.mcpserver.exceptions`), never
  `ValueError` or other exceptions. mcp 2.x treats any other exception as a crash and replaces its
  message with a bare "Error executing tool X" — the helpful text (valid issue types, available
  transitions, "token expired, run setup again") never reaches the model. Only `ToolError`'s
  message is passed through. This silently broke every error message in 0.2.0–0.2.1.
- `_raise_for_status()` is the single error path: any Jira API response >= 400 raises `ToolError`
  with the status code and truncated response body. It also raises on an
  `X-Seraph-LoginReason: AUTHENTICATED_FAILED`/`AUTHENTICATION_DENIED` header, because a revoked or
  expired API token doesn't always 401 — endpoints that allow anonymous access (notably
  `/search/jql`) return 200 with empty results, which would otherwise read as "no tickets". Tools
  don't otherwise catch/wrap errors.
- Tools are registered with `@mcp.tool(...)` and use `pydantic.Field` for parameter descriptions,
  which MCPServer surfaces to MCP clients as the tool's input schema. Every tool also passes
  `annotations=_TOOL_HINTS[name]` — MCP `ToolAnnotations` built by `_hints()`, which clients use
  to e.g. auto-approve read-only tools and warn on destructive ones. Be honest in these:
  `destructive` means it can overwrite/replace existing data (status, assignee, saved config),
  not just add to it; `idempotent` means repeating the call changes nothing further
  (`add_remote_link` is, via `globalId`; `add_comment` isn't); `external=False` marks tools that
  only touch local state.
- Current tools: `setup_jira_connection` (validates + persists credentials, see Configuration
  above), `jira_connection_status` (read-only connection check), `check_for_updates` (compares
  the installed version — read via `importlib.metadata.version("waypoint")`, so it always
  matches whatever's actually installed rather than a hardcoded string — against
  `WAYPOINT_GITHUB_REPO`'s latest GitHub release; degrades gracefully with `update_available:
  None` if that env var isn't set yet, i.e. pre-publish. Since this is a local stdio process,
  not a background service, this is the only "notify devs of updates" mechanism available —
  it's checked on demand, not pushed), `search_tickets` (raw JQL search)
  and `my_open_tickets` (canned JQL:
  `assignee = currentUser() AND statusCategory != Done`, so callers don't need to write JQL for the
  common "what's on my plate" case) — both go through the shared `_search_issues()` and
  `_issue_summary()` helpers and return `{"total", "total_is_estimate", "returned", "has_more",
  "issues"}` rather than a bare list, so a capped result set is visible instead of silently looking
  complete. `/search/jql` (the replacement for the retired `/search`) returns no `total` at all, so
  `_search_issues()` pages with `nextPageToken` up to `max_results` (100 per page), derives
  `has_more` from whether Jira offered another page (exact), and takes `total` from
  `/search/approximate-count` (an estimate; `None` if that call fails — never a made-up number); each issue has `summary`, `status`, `issue_type`, `assignee`/`assignee_account_id`,
  `reporter`/`reporter_account_id`, `priority`, `created`, `updated` (keep `_issue_summary()` as the
  single source of truth for this shape — don't let the two tools drift apart), `get_ticket`
  (fetches `fields=*all&expand=names` and returns, besides the basics: labels, components,
  fix/affects versions, due date, resolution, environment, `parent` and `subtasks`,
  `linked_issues` with the relationship phrased from this ticket's side ("blocks", "is blocked
  by" — on a GET the *other* issue appears as `outwardIssue`/`inwardIssue` with the matching
  phrase), `remote_links` from `/issue/{key}/remotelink` (often the Confluence spec or a PR;
  optional, so a failure there returns `[]` rather than failing the read), and `custom_fields`:
  every non-empty `customfield_*` keyed by its display name from `names` and flattened by
  `_field_value()` (ADF → Markdown, options/users/versions → name, sprints → "Sprint 12
  (active)", cascading selects → "EU / Germany"; opaque objects are dropped). Acceptance
  criteria, story points, and sprint usually live there, not in the description. `Rank`,
  `Development`, and `[CHART]` fields are skipped as noise. Attachments include their `id` for
  `download_attachment`. It also fetches all
  comments via the dedicated paginated `/issue/{key}/comment` endpoint through `_fetch_all_comments()`
  — the comment array embedded in `fields=*all` is capped at Jira's default page size, so relying on
  it alone would silently drop comments on a busy ticket; each comment includes flattened `body`
  text, the raw `body_adf` JSON, and `author_account_id`), `list_issue_types` (lists every issue
  type in a project via the shared `_fetch_project_issue_types()` helper, each flagged
  `subtask: bool` — the one discovery tool behind both `create_ticket` and `create_subtask`'s
  type validation, mirroring how `get_available_transitions`/`list_link_types` already make
  callers check valid options instead of guessing), `create_ticket` (validates `issue_type`
  against the project's non-subtask issue types before creating anything, raising a clear error
  listing the valid names on a mismatch instead of letting Jira reject it deeper in the call),
  `create_subtask` (looks up the parent's project automatically; its optional `issue_type` param
  is required whenever the project has more than one sub-task type — it raises rather than
  silently picking one, since guessing here previously created real tickets with the wrong
  type, e.g. 'Story Bug' instead of 'Dev'), `add_comment` (its description explicitly asks the
  model to write like a developer's quick note, not formal/robotic phrasing, and to call
  `add_attachment` for any relevant screenshot instead of only describing it in text),
  `get_available_transitions`
  (lists valid status transitions for a ticket — status names are workflow-specific per project,
  e.g. one project used 'Started' instead of the more common 'In Progress', so check this rather
  than guessing), `update_ticket_status` (resolves to a
  transition id via `/transitions?expand=transitions.fields` first — Jira requires the transition
  id to change status. A transition's own name, e.g. 'Start Progress', and the status it leads to,
  e.g. 'In Progress', are different strings, so it matches the transition name first and then the
  destination status, refusing to pick when several transitions lead to the same status. If the
  transition screen requires fields with no default, it fills `resolution` from its optional
  param and otherwise raises an error naming the required fields and their allowed values, rather
  than letting Jira reject the POST), `update_ticket_assignee`/`add_watcher` (resolve an
  email, display name, *or* accountId via the shared `_resolve_account_id()` helper — it tries the
  input as a literal accountId first via `GET /user`, which works regardless of site privacy
  settings, before falling back to `/user/search`; that fallback returns nothing on sites with
  GDPR/privacy-mode user search restricted, common on enterprise Jira, so callers who already have
  an accountId — e.g. from `list_watchers`'s or `get_ticket`'s `*_account_id` fields — can bypass
  search entirely. `/user/search` is a fuzzy prefix match, so it ignores inactive users and only
  accepts a single result or a single exact email/display-name match; otherwise it raises listing
  the candidates' accountIds instead of assigning whoever came back first), `add_worklog` (time_spent is optional: if omitted, it requires an active
  `set_working_issue` timer for that ticket and auto-computes elapsed wall-clock time via
  `_fetch_time_tracking_config()` — reading the site's actual `workingHoursPerDay`/
  `workingDaysPerWeek` from `/configuration`, defaulting to 8/5 — and `_format_duration_jira()`;
  this is calendar elapsed time, not verified focus time, so the tool description explicitly warns
  callers to sanity-check it before trusting it for anything that spans a break), `list_projects`
  (project discovery — pick a valid key before `create_ticket`), `set_project_workspace`/
  `get_project_workspace` (persists a Jira project key → `{repo_path, subdirectory}` mapping in
  `_project_repos`/`JIRA_PROJECT_REPOS`, so `implement_ticket`'s workspace check can be
  deterministic after the first confirmation instead of re-guessing from the git remote every
  time), `add_attachment` (multipart upload via a one-off `httpx.AsyncClient`, not `_client()` —
  its default `Content-Type: application/json` header would break the multipart body),
  `download_attachment` (streams `/attachment/content/{id}` to a local file — a per-attachment
  folder under the system temp dir by default, never overwriting an existing file — and for
  PNG/JPEG/GIF/WebP up to 5 MB also returns an `Image` so the model sees the screenshot inline
  via an MCP image content block. That endpoint redirects to Atlassian's media host with a
  signed URL; httpx drops `Authorization` on the cross-origin redirect, which is required —
  don't replace it with a client that forwards credentials. Attachment filenames are untrusted,
  so `_safe_filename()` strips path components and leading dots; ids must be numeric; downloads
  cap at 100 MB),
  `list_watchers` (returns `display_name`+`account_id` per watcher), `list_link_types`/
  `link_tickets` (creates an `/issueLink` between two tickets; call `list_link_types` first since
  valid names are site-specific), `add_remote_link` (POSTs `/issue/{key}/remotelink` — a web link
  in the ticket's Links panel, used by `implement_ticket` to link the pushed PR/branch/commit.
  `globalId` is `url=<url>` (sha256 of the URL if that would exceed Jira's 255-char limit), and
  Jira updates rather than duplicates a link with an existing `globalId`, so re-running the
  workflow is idempotent; 201 means created, 200 updated. Only http(s) URLs are accepted, and
  github.com/gitlab.com/bitbucket.org links get an `application` so Jira groups them. This is a
  plain web link, not Jira's Development panel — that panel is fed only by a site's forge
  integration app matching the issue key in branch/commit/PR names, which no REST call here can
  write to; hence the prompt's insistence on keeping the key in all three),
  `list_favorite_filters`, and `set_working_issue`/
  `get_working_issue` (in-memory `_working_issue` dict — `{"key": ..., "started_at": ...}`,
  session-scoped only, resets on server restart, no persistence by design; `started_at` backs
  `add_worklog`'s auto-elapsed-time feature above).
  Every tool that returns a ticket key also returns a `url` (`{site_url}/browse/{key}`).
  `create_ticket` accepts optional `priority`/`labels`/`components` (no fix-version support yet).
- Two MCP **prompts** are defined separately from tools via `@mcp.prompt(...)` — each returns a
  scripted instruction message (not a tool call) directing the calling model through a multi-step
  workflow using the tools above. Prompts and tools are distinct MCP primitives; don't conflate the
  two when adding new capabilities.
  - `tour`: a no-argument onboarding walkthrough for a new user — checks connection status,
    calls `check_for_updates` in passing, explains the server's scope, shows real data from
    `my_open_tickets`, and explains `implement_ticket`/`plan_ticket`. Meant to be the first
    thing a new user runs.
  - `plan_ticket`: read a ticket, assess scope, and create sub-tasks under it — told to check
    `list_issue_types` and pick the right type explicitly (e.g. 'Dev') rather than letting
    `create_subtask` guess when a project has more than one.
  - `implement_ticket`: the primary intended workflow for this server — read a ticket end to end,
    verify the workspace (`get_project_workspace` first for a deterministic match, falling back to
    checking `git remote -v`/directory name and asking the user to confirm if it can't tell, then
    saving the mapping via `set_project_workspace` for next time), and either post a comment
    flagging what's unclear (stopping short of writing code), or actually implement it: mark it as
    the working issue, transition its status, analyze the existing code before drafting a plan,
    write the code in the relevant project using normal file/code tools (not a Jira tool —
    implementation happens outside this server entirely), verify it (tests/build/lint) and stop
    without committing if verification still fails after a reasonable fix attempt, comment a
    summary back in plain developer language (not formal/robotic phrasing) with any relevant
    screenshot attached via `add_attachment` rather than only described in text, and log time via
    `add_worklog` (auto-computed elapsed time by default), bias the
    next status toward a review-style status rather than done/closed, then — after checking the
    repo's existing branch-naming convention — ask the user whether to commit directly or on a new
    branch, stage only the files this task touched (never a blanket add), commit as
    `{ISSUE-KEY}: <summary>` via normal git tools, ask again before pushing (a separate
    confirmation even if the user already chose "commit directly", since push is harder to reverse
    and affects shared state), offer to open a PR/MR (title prefixed `{ISSUE-KEY}: `) if a new
    branch was pushed, and finally link whatever was actually pushed — PR, else branch, else
    commit — back on the ticket via `add_remote_link`, never a local-only commit. Branch names
    keep the issue key even when following the repo's own convention, so a site's forge
    integration can populate Jira's Development panel too.

  **Design intent:** this server is scoped for a single developer working one ticket at a time
  inside their editor (fetch → analyze → flag gap or implement → status/worklog as a side effect of
  work already happening), not for broad Jira/PM administration. Official and community Jira MCP
  servers already cover Agile boards/sprints/epics far more completely than this one ever aims to;
  don't add that surface area here — it would be scope creep away from what this server is for.

## Adding a new tool

Follow the existing pattern: decorate an `async def` with `@mcp.tool(name=..., description=...)`,
type parameters with `pydantic.Field` for descriptions/defaults, use `_client()` for the HTTP call,
call `await _raise_for_status(response)` before parsing, raise `ToolError` (not `ValueError`) for
anything the model should read and act on, and return a plain dict/list (not the raw Jira JSON)
shaped to what a model actually needs. Any free text the tool sends to Jira should go through
`_adf_from_markdown()`, and any ADF it returns through `_text_from_adf()`. Add the tool to
`_TOOL_HINTS` with accurate annotations, and if it changes Jira, to `_JIRA_WRITE_TOOLS` too (the
tests fail until both are done). Add tests using the `jira` fixture, and run `uv run pytest`.
