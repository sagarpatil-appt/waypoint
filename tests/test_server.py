import ast
import os
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

import waypoint_server
from conftest import ROOT, TEST_ENV, call

SERVER = str(ROOT / "waypoint_server.py")
# Tools that don't change Jira but aren't read-only either: they only touch local state.
LOCAL_STATE_TOOLS = {"setup_jira_connection", "set_project_workspace", "set_working_issue", "download_attachment"}


@pytest.mark.anyio
async def test_every_registered_tool_has_annotations_and_vice_versa():
    tools = await waypoint_server.mcp.list_tools()
    registered = {t.name for t in tools}
    assert registered == set(waypoint_server._TOOL_HINTS)
    for tool in tools:
        assert tool.annotations == waypoint_server._TOOL_HINTS[tool.name], tool.name


def test_every_non_read_only_tool_is_classified():
    """A new tool that changes something must be either a Jira write (hidden in read-only mode)
    or an explicitly local-state tool — never silently left enabled in read-only mode."""
    writes = {name for name, hints in waypoint_server._TOOL_HINTS.items() if not hints.read_only_hint}
    assert writes == waypoint_server._JIRA_WRITE_TOOLS | LOCAL_STATE_TOOLS
    assert not (waypoint_server._JIRA_WRITE_TOOLS & LOCAL_STATE_TOOLS)


def test_no_tool_raises_value_error():
    """mcp 2.x only passes a ToolError's message to the model; any other exception reaches it as
    a bare "Error executing tool X". This hid every error message in 0.2.0-0.2.1."""
    tree = ast.parse((ROOT / "waypoint_server.py").read_text())
    raised = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and getattr(node.exc.func, "id", None) == "ValueError"
    ]
    assert raised == [], f"raise ToolError instead of ValueError at lines {raised}"


@pytest.mark.anyio
async def test_rejected_token_is_an_error_not_an_empty_result(jira):
    """Jira answers a revoked token on /search/jql with 200 + no issues, flagged only by a header."""
    jira.on("GET", "/search/jql", json={"issues": [], "isLast": True}, headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"})
    with pytest.raises(ToolError, match="rejected the saved credentials"):
        await call("my_open_tickets")


@pytest.mark.anyio
async def test_unconfigured_server_points_at_setup():
    with pytest.raises(ToolError, match="setup_jira_connection"):
        await call("get_ticket", issue_key="ABC-1")


@pytest.mark.anyio
async def test_setup_rejects_bad_credentials(monkeypatch):
    def transport(request):
        return httpx.Response(401, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        waypoint_server.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(transport)}),
    )
    with pytest.raises(ToolError, match="401 Unauthorized"):
        await call("setup_jira_connection", site_url="acme.atlassian.net", email="a@b.c", api_token="bad")
    assert waypoint_server._config["api_token"] == ""


def _stdio_params(extra_env=None):
    env = {**os.environ, **TEST_ENV, **(extra_env or {})}
    return StdioServerParameters(command=sys.executable, args=[SERVER], env=env)


async def _list_over_stdio(extra_env=None):
    params = _stdio_params(extra_env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            error = await session.call_tool("download_attachment", {"attachment_id": "abc"}) if "download_attachment" in tools else None
            blocked = await session.call_tool("add_comment", {"issue_key": "ABC-1", "comment": "x"})
            return init, tools, error, blocked


@pytest.mark.anyio
async def test_stdio_server_starts_and_surfaces_error_messages():
    init, tools, error, _ = await _list_over_stdio()
    assert init.server_info.name == "Waypoint"
    assert init.server_info.version == waypoint_server._VERSION
    assert tools == set(waypoint_server._TOOL_HINTS)
    assert error.is_error and "not an attachment id" in error.content[0].text


@pytest.mark.anyio
async def test_read_only_mode_hides_and_blocks_jira_writes():
    init, tools, _, blocked = await _list_over_stdio({"WAYPOINT_READ_ONLY": "true"})
    assert tools == set(waypoint_server._TOOL_HINTS) - waypoint_server._JIRA_WRITE_TOOLS
    assert blocked.is_error  # not just unlisted: uncallable too
    assert "read-only mode" in init.instructions


@pytest.mark.anyio
async def test_enabled_and_disabled_tool_lists():
    _, tools, _, _ = await _list_over_stdio({"WAYPOINT_ENABLED_TOOLS": "get_ticket, my_open_tickets"})
    assert tools == {"get_ticket", "my_open_tickets"} | waypoint_server._ALWAYS_AVAILABLE_TOOLS

    _, tools, _, blocked = await _list_over_stdio({"WAYPOINT_DISABLED_TOOLS": "add_comment,add_worklog"})
    assert tools == set(waypoint_server._TOOL_HINTS) - {"add_comment", "add_worklog"}
    assert blocked.is_error


def test_unknown_tool_name_stops_the_server():
    env = {**os.environ, **TEST_ENV, "WAYPOINT_DISABLED_TOOLS": "add_coment"}
    out = subprocess.run([sys.executable, SERVER], env=env, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert "unknown tool name(s)" in out.stderr and "add_coment" in out.stderr
