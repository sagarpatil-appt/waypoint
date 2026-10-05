import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import adf_errors, call, issue_fields

pytestmark = pytest.mark.anyio


def doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


TICKET = {
    "key": "ABC-7",
    "names": {
        "customfield_100": "Acceptance Criteria", "customfield_101": "Story Points",
        "customfield_102": "Sprint", "customfield_103": "Rank", "customfield_104": "Team",
        "customfield_105": "[CHART] Time in Status", "customfield_106": "Empty Field",
        "customfield_107": "Region", "customfield_108": "Development",
    },
    "fields": issue_fields(
        description=doc(para("Safari users get a 500 after login.")),
        labels=["safari"], components=[{"name": "web"}], fixVersions=[{"name": "2.4"}], versions=[],
        duedate="2026-10-15", resolution=None, environment=None,
        parent={"key": "ABC-1", "fields": {"summary": "Auth epic", "status": {"name": "In Progress"}, "issuetype": {"name": "Epic"}}},
        subtasks=[{"key": "ABC-8", "fields": {"summary": "Write test", "status": {"name": "To Do"}, "issuetype": {"name": "Dev"}}}],
        issuelinks=[
            {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
             "outwardIssue": {"key": "ABC-9", "fields": {"summary": "Release", "status": {"name": "To Do"}}}},
            {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
             "inwardIssue": {"key": "ABC-3", "fields": {"summary": "Upgrade SDK", "status": {"name": "Done"}}}},
        ],
        customfield_100=doc(para("Given an expired session, login succeeds")),
        customfield_101=3.0,
        customfield_102=[{"id": 1, "name": "Sprint 12", "state": "active", "boardId": 4}],
        customfield_103="0|i0001:",
        customfield_104={"id": "t1", "name": "Platform"},
        customfield_105="noise",
        customfield_106=None,
        customfield_107={"value": "EU", "child": {"value": "Germany"}},
        customfield_108="{pullrequest={dataType=pullrequest, state=OPEN}}",
        attachment=[{"id": "10001", "filename": "error.png", "size": 68, "mimeType": "image/png",
                     "content": "https://acme.atlassian.net/rest/api/3/attachment/content/10001"}],
    ),
}


@pytest.fixture
def ticket(jira):
    jira.on("GET", "/issue/ABC-7", json=TICKET)
    jira.on("GET", "/issue/ABC-7/comment", json={"comments": [
        {"author": {"displayName": "Priya", "accountId": "acc-p"}, "body": doc(para("Repro on iOS 18 too"))}], "total": 1})
    jira.on("GET", "/issue/ABC-7/remotelink", json=[
        {"relationship": "mentioned in", "object": {"title": "Login spec", "url": "https://acme.atlassian.net/wiki/x"},
         "application": {"name": "Confluence"}}])
    return jira


async def test_get_ticket_returns_full_context(ticket):
    t = await call("get_ticket", issue_key="ABC-7")
    assert t["description"] == "Safari users get a 500 after login."
    assert t["comments"][0]["body"] == "Repro on iOS 18 too"
    assert t["parent"] == {"key": "ABC-1", "summary": "Auth epic", "status": "In Progress", "issue_type": "Epic"}
    assert [s["key"] for s in t["subtasks"]] == ["ABC-8"]
    assert {(l["relationship"], l["key"]) for l in t["linked_issues"]} == {("blocks", "ABC-9"), ("is blocked by", "ABC-3")}
    assert t["remote_links"] == [{"title": "Login spec", "url": "https://acme.atlassian.net/wiki/x",
                                  "relationship": "mentioned in", "application": "Confluence"}]
    assert (t["labels"], t["components"], t["fix_versions"], t["due_date"]) == (["safari"], ["web"], ["2.4"], "2026-10-15")
    assert t["attachments"][0]["id"] == "10001"
    assert ticket.sent("GET", "/issue/ABC-7")[0].url.params["expand"] == "names"


async def test_custom_fields_are_named_flattened_and_denoised(ticket):
    assert (await call("get_ticket", issue_key="ABC-7"))["custom_fields"] == {
        "Acceptance Criteria": "Given an expired session, login succeeds",
        "Story Points": 3.0,
        "Sprint": ["Sprint 12 (active)"],
        "Team": "Platform",
        "Region": "EU / Germany",
    }


async def test_remote_link_failure_does_not_fail_the_read(ticket):
    ticket.on("GET", "/issue/ABC-7/remotelink", status=403, json={})
    assert (await call("get_ticket", issue_key="ABC-7"))["remote_links"] == []


async def test_comments_are_fully_paginated(jira):
    jira.on("GET", "/issue/ABC-7", json=TICKET)
    jira.on("GET", "/issue/ABC-7/remotelink", json=[])

    def comments(request):
        start = int(request.url.params["startAt"])
        page = [{"author": {"displayName": "x"}, "body": doc(para(f"c{i}"))} for i in range(start, min(start + 100, 250))]
        return httpx.Response(200, json={"comments": page, "total": 250})

    jira.on("GET", "/issue/ABC-7/comment", comments)
    assert len((await call("get_ticket", issue_key="ABC-7"))["comments"]) == 250


@pytest.fixture
def project(jira):
    jira.on("GET", "/project/ABC", json={"issueTypes": [
        {"id": "1", "name": "Task", "subtask": False}, {"id": "2", "name": "Bug", "subtask": False},
        {"id": "3", "name": "Dev", "subtask": True}, {"id": "4", "name": "Story Bug", "subtask": True}]})
    jira.on("POST", "/issue", status=201, json={"key": "ABC-99", "id": "99"})
    jira.on("GET", "/issue/ABC-7", json={"key": "ABC-7", "fields": {"project": {"key": "ABC"}}})
    return jira


async def test_create_ticket_sends_markdown_as_valid_adf(project, adf_validator):
    result = await call("create_ticket", project_key="ABC", summary="Fix login",
                        description="## Repro\n1. open `/login`\n2. **submit**", issue_type="bug")
    assert result == {"key": "ABC-99", "url": "https://acme.atlassian.net/browse/ABC-99"}
    fields = project.sent_json("POST", "/issue")[0]["fields"]
    assert fields["issuetype"] == {"name": "Bug"}
    assert adf_errors(adf_validator, fields["description"]) == []
    assert [b["type"] for b in fields["description"]["content"]] == ["heading", "orderedList"]


async def test_create_ticket_rejects_unknown_type_listing_valid_ones(project):
    with pytest.raises(ToolError, match="Available: Task, Bug"):
        await call("create_ticket", project_key="ABC", summary="x", issue_type="Story")
    assert project.sent("POST", "/issue") == []


async def test_create_subtask_refuses_to_guess_between_subtask_types(project):
    with pytest.raises(ToolError, match=r"multiple sub-task types \(Dev, Story Bug\)"):
        await call("create_subtask", parent_key="ABC-7", summary="x")
    await call("create_subtask", parent_key="ABC-7", summary="x", issue_type="dev")
    assert project.sent_json("POST", "/issue")[0]["fields"]["issuetype"] == {"name": "Dev"}


async def test_comment_and_worklog_send_valid_adf(jira, adf_validator):
    jira.on("POST", "/issue/ABC-7/comment", status=201, json={"id": "1"})
    jira.on("POST", "/issue/ABC-7/worklog", status=201, json={"id": "5", "timeSpent": "1h"})
    await call("add_comment", issue_key="ABC-7", comment="Fixed:\n\n- guard in `auth.ts`\n\n```ts\nif (!t) return;\n```")
    await call("add_worklog", issue_key="ABC-7", time_spent="1h", comment="Paired on the *fix*")
    comment = jira.sent_json("POST", "/issue/ABC-7/comment")[0]["body"]
    worklog = jira.sent_json("POST", "/issue/ABC-7/worklog")[0]
    assert adf_errors(adf_validator, comment) == [] and adf_errors(adf_validator, worklog["comment"]) == []
    assert [b["type"] for b in comment["content"]] == ["paragraph", "bulletList", "codeBlock"]
    assert worklog["timeSpent"] == "1h"


TRANSITIONS = [
    {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}, "fields": {}},
    {"id": "21", "name": "Close", "to": {"name": "Done"}, "fields": {"resolution": {
        "name": "Resolution", "required": True, "hasDefaultValue": False,
        "allowedValues": [{"name": "Fixed"}, {"name": "Won't Do"}]}}},
    {"id": "31", "name": "Send to QA", "to": {"name": "In Review"}, "fields": {}},
    {"id": "32", "name": "Fast-track review", "to": {"name": "In Review"}, "fields": {}},
]


@pytest.fixture
def transitions(jira):
    jira.on("GET", "/issue/ABC-7/transitions", json={"transitions": TRANSITIONS})
    jira.on("POST", "/issue/ABC-7/transitions", status=204)
    return jira


@pytest.mark.parametrize("status", ["In Progress", "start progress"])
async def test_status_matches_destination_or_transition_name(transitions, status):
    result = await call("update_ticket_status", issue_key="ABC-7", status=status)
    assert transitions.sent_json("POST", "/issue/ABC-7/transitions")[-1] == {"transition": {"id": "11"}}
    assert result["status"] == "In Progress" and result["transition"] == "Start Progress"


async def test_status_refuses_ambiguous_destination(transitions):
    with pytest.raises(ToolError, match="Send to QA, Fast-track review"):
        await call("update_ticket_status", issue_key="ABC-7", status="In Review")
    assert transitions.sent("POST", "/issue/ABC-7/transitions") == []


async def test_status_reports_required_resolution_then_accepts_it(transitions):
    with pytest.raises(ToolError, match=r"requires: Resolution \(options: Fixed, Won't Do\)"):
        await call("update_ticket_status", issue_key="ABC-7", status="Done")
    await call("update_ticket_status", issue_key="ABC-7", status="Done", resolution="Fixed")
    assert transitions.sent_json("POST", "/issue/ABC-7/transitions")[-1] == {
        "transition": {"id": "21"}, "fields": {"resolution": {"name": "Fixed"}}}


async def test_status_unknown_lists_transition_to_status_pairs(transitions):
    with pytest.raises(ToolError, match="Start Progress → In Progress"):
        await call("update_ticket_status", issue_key="ABC-7", status="Nope")


USERS = [
    {"accountId": "1", "displayName": "Sam Lee", "emailAddress": "sam@acme.test", "active": True},
    {"accountId": "2", "displayName": "Samantha Ray", "active": True},
    {"accountId": "3", "displayName": "Sam Old", "active": False},
]


@pytest.fixture
def users(jira):
    jira.on("GET", "/user/search", json=USERS)
    jira.on("PUT", "/issue/ABC-7/assignee", status=204)
    return jira


async def test_ambiguous_user_is_refused_with_candidates(users):
    with pytest.raises(ToolError) as error:
        await call("update_ticket_assignee", issue_key="ABC-7", assignee="Sam")
    assert "Sam Lee (1)" in str(error.value) and "Samantha Ray (2)" in str(error.value)
    assert "Sam Old" not in str(error.value)
    assert users.sent("PUT", "/issue/ABC-7/assignee") == []


@pytest.mark.parametrize("who", ["sam lee", "SAM@acme.test"])
async def test_exact_name_or_email_resolves(users, who):
    await call("update_ticket_assignee", issue_key="ABC-7", assignee=who)
    assert users.sent_json("PUT", "/issue/ABC-7/assignee") == [{"accountId": "1"}]


@pytest.mark.parametrize("account_id", [
    "5d53f3cbc6b9320d9ea5bdc2",
    "557058:f58131cb-b67d-43c7-b30d-6b58d40bd077",
    "qm:a713c8ea-1075-4e30-9d96-891a7d181739:5ad6d3581db05e2a66fa80b",
])
async def test_account_id_skips_search(jira, account_id):
    jira.on("GET", "/user", json={"accountId": account_id, "displayName": "Kim"})
    jira.on("PUT", "/issue/ABC-7/assignee", status=204)
    result = await call("update_ticket_assignee", issue_key="ABC-7", assignee=account_id)
    assert result["assignee"] == "Kim" and jira.sent("GET", "/user/search") == []
    assert jira.sent("GET", "/user")[0].url.params["accountId"] == account_id


async def test_empty_assignee_unassigns(jira):
    jira.on("PUT", "/issue/ABC-7/assignee", status=204)
    await call("update_ticket_assignee", issue_key="ABC-7")
    assert jira.sent_json("PUT", "/issue/ABC-7/assignee") == [{"accountId": None}]
