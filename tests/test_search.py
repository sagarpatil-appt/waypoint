import httpx
import pytest

from conftest import call, issue_fields

pytestmark = pytest.mark.anyio

ALL_ISSUES = [{"key": f"ABC-{i}", "fields": issue_fields()} for i in range(150)]


def paged_search(request):
    """Mimic /search/jql: up to 100 per page, an opaque nextPageToken, no `total`."""
    start = int(request.url.params.get("nextPageToken") or 0)
    end = min(start + min(int(request.url.params["maxResults"]), 100), len(ALL_ISSUES))
    body = {"issues": ALL_ISSUES[start:end], "isLast": end >= len(ALL_ISSUES)}
    if end < len(ALL_ISSUES):
        body["nextPageToken"] = str(end)
    return httpx.Response(200, json=body)


@pytest.fixture
def search(jira):
    jira.on("GET", "/search/jql", paged_search)
    jira.on("POST", "/search/approximate-count", json={"count": 150})
    return jira


async def test_capped_result_reports_has_more_and_estimated_total(search):
    result = await call("search_tickets", jql="project = ABC", max_results=20)
    assert (result["returned"], result["has_more"], result["total"]) == (20, True, 150)
    assert result["total_is_estimate"] is True


async def test_pages_through_all_results(search):
    result = await call("search_tickets", jql="project = ABC", max_results=500)
    assert result["returned"] == 150 and result["has_more"] is False
    assert len({issue["key"] for issue in result["issues"]}) == 150
    assert len(search.sent("GET", "/search/jql")) == 2


async def test_cap_inside_second_page(search):
    result = await call("search_tickets", jql="project = ABC", max_results=120)
    assert result["returned"] == 120 and result["has_more"] is True


async def test_total_is_null_when_count_unavailable_never_invented(jira):
    jira.on("GET", "/search/jql", json={"issues": [{"key": "ABC-1", "fields": issue_fields()}], "isLast": True})
    jira.on("POST", "/search/approximate-count", status=400, json={"errorMessages": ["nope"]})
    result = await call("my_open_tickets")
    assert result["total"] is None and result["returned"] == 1 and result["has_more"] is False


async def test_empty_page_with_token_does_not_loop_forever(jira):
    jira.on("GET", "/search/jql", json={"issues": [], "nextPageToken": "again", "isLast": False})
    jira.on("POST", "/search/approximate-count", json={"count": 0})
    result = await call("search_tickets", jql="project = ABC", max_results=50)
    assert result["returned"] == 0
    assert len(jira.sent("GET", "/search/jql")) == 1


async def test_my_open_tickets_uses_canned_jql(search):
    await call("my_open_tickets", max_results=5)
    jql = search.sent("GET", "/search/jql")[0].url.params["jql"]
    assert jql == "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"


async def test_issue_summary_shape(search):
    issue = (await call("search_tickets", jql="x", max_results=1))["issues"][0]
    assert issue == {
        "key": "ABC-0",
        "url": "https://acme.atlassian.net/browse/ABC-0",
        "summary": "Login 500s on Safari",
        "status": "To Do",
        "issue_type": "Bug",
        "assignee": None,
        "assignee_account_id": None,
        "reporter": "Lee",
        "reporter_account_id": "acc-lee",
        "priority": "High",
        "created": "2026-09-01T10:00:00.000+0000",
        "updated": "2026-09-02T10:00:00.000+0000",
    }
