import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

# Isolate the server from the developer's real config before it's imported: a throwaway config
# dir (with an empty .env, so the legacy-file migration never copies a real .env into it), and
# empty JIRA_* vars, which load_dotenv won't override — so no real credentials are ever loaded.
_CONFIG_HOME = Path(tempfile.mkdtemp(prefix="waypoint-test-config-"))
(_CONFIG_HOME / "waypoint").mkdir()
(_CONFIG_HOME / "waypoint" / ".env").write_text("")
TEST_ENV = {
    "XDG_CONFIG_HOME": str(_CONFIG_HOME),
    "APPDATA": str(_CONFIG_HOME),
    "JIRA_SITE_URL": "",
    "JIRA_EMAIL": "",
    "JIRA_API_TOKEN": "",
    "JIRA_PROJECT_REPOS": "{}",
}
os.environ.update(TEST_ENV)
for name in ("WAYPOINT_READ_ONLY", "WAYPOINT_ENABLED_TOOLS", "WAYPOINT_DISABLED_TOOLS"):
    os.environ.pop(name, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import waypoint_server  # noqa: E402

SITE = "https://acme.atlassian.net"
API = "/rest/api/3"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeJira:
    """A tiny in-memory Jira: register responses per (method, path) and inspect what was sent.

    Paths are relative to /rest/api/3 (e.g. "/issue/ABC-1"), or a full URL for other hosts.
    A route's response can be a callable taking the httpx.Request, for dynamic behaviour."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, path, response=None, *, json=None, status=200, headers=None, content=None):
        if response is None:
            response = httpx.Response(status, json=json, headers=headers, content=content)
        self.routes.append((method.upper(), path, response))
        return self

    def sent(self, method, path):
        """Requests sent to a route, newest last."""
        return [r for r in self.requests if r.method == method.upper() and self._matches(path, r)]

    def sent_json(self, method, path):
        return [json.loads(r.content) for r in self.sent(method, path)]

    @staticmethod
    def _matches(path, request):
        if path.startswith("http"):
            return f"{request.url.scheme}://{request.url.host}{request.url.path}" == path
        return request.url.host == "acme.atlassian.net" and request.url.path == API + path

    def handle(self, request):
        self.requests.append(request)
        for method, path, response in reversed(self.routes):  # latest registration wins
            if method == request.method and self._matches(path, request):
                return response(request) if callable(response) else response
        return httpx.Response(404, json={"errorMessages": [f"FakeJira: no route for {request.method} {request.url}"]})



@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()

    def client():
        return httpx.AsyncClient(
            base_url=SITE + API,
            auth=("dev@acme.test", "test-token"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=5.0,
            transport=httpx.MockTransport(fake.handle),
        )

    monkeypatch.setattr(waypoint_server, "_client", client)
    monkeypatch.setitem(waypoint_server._config, "site_url", SITE)
    monkeypatch.setitem(waypoint_server._config, "email", "dev@acme.test")
    monkeypatch.setitem(waypoint_server._config, "api_token", "test-token")
    return fake


async def call(name, **arguments):
    """Call a tool through the MCP server (argument validation, defaults, error surfacing) and
    return its text content parsed as JSON — one value, or a list if the tool returned several
    content blocks (as list-returning tools do)."""
    result = await waypoint_server.mcp.call_tool(name, arguments)
    values = [json.loads(c.text) for c in result.content if c.type == "text"]
    return values[0] if len(values) == 1 else values


@pytest.fixture(scope="session")
def adf_validator():
    import jsonschema

    schema = json.loads((ROOT / "tests" / "fixtures" / "adf-schema-v1.json").read_text())
    return jsonschema.Draft4Validator(schema)


def adf_errors(validator, document):
    return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(document)]


def issue_fields(**overrides):
    fields = {
        "summary": "Login 500s on Safari",
        "status": {"name": "To Do"},
        "issuetype": {"name": "Bug"},
        "project": {"key": "ABC"},
        "assignee": None,
        "reporter": {"displayName": "Lee", "accountId": "acc-lee"},
        "priority": {"name": "High"},
        "created": "2026-09-01T10:00:00.000+0000",
        "updated": "2026-09-02T10:00:00.000+0000",
    }
    fields.update(overrides)
    return fields
