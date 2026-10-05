import base64
import json
from pathlib import Path

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import waypoint_server
from conftest import call

pytestmark = pytest.mark.anyio

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def attachments(jira):
    jira.on("GET", "/attachment/10001", json={"filename": "../../.ssh/error.png", "size": len(PNG), "mimeType": "image/png"})
    jira.on("GET", "/attachment/content/10001", status=303,
            headers={"Location": "https://media.example.com/file/abc?token=signed"})
    jira.on("GET", "https://media.example.com/file/abc", content=PNG)
    jira.on("GET", "/attachment/10002", json={"filename": "server.log", "size": 11, "mimeType": "text/plain"})
    jira.on("GET", "/attachment/content/10002", content=b"ERROR boom\n")
    return jira


async def test_image_is_saved_and_returned_inline(attachments, tmp_path):
    result = await waypoint_server.mcp.call_tool("download_attachment", {"attachment_id": "10001", "save_dir": str(tmp_path)})
    assert [c.type for c in result.content] == ["text", "image"]
    assert result.content[1].mime_type == "image/png"
    info = json.loads(result.content[0].text)
    saved = Path(info["saved_to"])
    assert saved == tmp_path / "error.png"  # path components and leading dots stripped
    assert saved.read_bytes() == PNG


async def test_credentials_are_not_forwarded_to_the_media_host(attachments, tmp_path):
    await call("download_attachment", attachment_id="10001", save_dir=str(tmp_path), show_image=False)
    media = attachments.sent("GET", "https://media.example.com/file/abc")[0]
    jira = attachments.sent("GET", "/attachment/content/10001")[0]
    assert "authorization" not in {k.lower() for k in media.headers}
    assert "authorization" in {k.lower() for k in jira.headers}


async def test_existing_files_are_never_overwritten(attachments, tmp_path):
    (tmp_path / "error.png").write_bytes(b"mine")
    info = await call("download_attachment", attachment_id="10001", save_dir=str(tmp_path), show_image=False)
    assert info["saved_to"].endswith("error (1).png")
    assert (tmp_path / "error.png").read_bytes() == b"mine"


async def test_non_image_saved_to_temp_dir_without_inline_content(attachments, tmp_path, monkeypatch):
    monkeypatch.setattr(waypoint_server.tempfile, "gettempdir", lambda: str(tmp_path))
    result = await waypoint_server.mcp.call_tool("download_attachment", {"attachment_id": "10002"})
    assert [c.type for c in result.content] == ["text"]
    saved = Path(json.loads(result.content[0].text)["saved_to"])
    assert saved.parent == tmp_path / "waypoint-attachments" / "10002"
    assert saved.read_text() == "ERROR boom\n"


@pytest.mark.parametrize("bad_id", ["../etc", "abc", ""])
async def test_non_numeric_ids_rejected_before_any_request(jira, bad_id):
    with pytest.raises(ToolError, match="not an attachment id"):
        await call("download_attachment", attachment_id=bad_id)
    assert jira.requests == []


async def test_oversized_attachment_refused(jira):
    jira.on("GET", "/attachment/10003", json={"filename": "huge.zip", "size": 500 * 1024 * 1024, "mimeType": "application/zip"})
    with pytest.raises(ToolError, match="over the 100 MB download limit"):
        await call("download_attachment", attachment_id="10003")
    assert jira.sent("GET", "/attachment/content/10003") == []


@pytest.mark.parametrize("name, expected", [
    ("../../.ssh/config", "config"),
    ("..\\..\\evil.bat", "evil.bat"),
    (".env", "env"),
    ("", "fallback"),
    ("report.pdf", "report.pdf"),
])
def test_safe_filename(name, expected):
    assert waypoint_server._safe_filename(name, "fallback") == expected


@pytest.fixture
def remote_links(jira):
    store = {}

    def remotelink(request):
        body = json.loads(request.content)
        existed = body["globalId"] in store
        store[body["globalId"]] = body
        return httpx.Response(200 if existed else 201, json={"id": len(store)})

    jira.on("POST", "/issue/ABC-7/remotelink", remotelink)
    jira.store = store
    return jira


async def test_remote_link_payload(remote_links):
    pr = "https://github.com/acme/web/pull/42"
    result = await call("add_remote_link", issue_key="ABC-7", url=pr, title="PR #42: Fix login", relationship="pull request")
    assert remote_links.sent_json("POST", "/issue/ABC-7/remotelink")[0] == {
        "globalId": f"url={pr}",
        "object": {"url": pr, "title": "PR #42: Fix login"},
        "relationship": "pull request",
        "application": {"type": "com.github", "name": "GitHub"},
    }
    assert result["status"] == "linked"


async def test_relinking_same_url_updates_instead_of_duplicating(remote_links):
    pr = "https://github.com/acme/web/pull/42"
    await call("add_remote_link", issue_key="ABC-7", url=pr, title="PR #42")
    again = await call("add_remote_link", issue_key="ABC-7", url=pr + " ", title="PR #42 renamed", summary="with tests")
    assert again["status"] == "updated existing link"
    assert len(remote_links.store) == 1


async def test_long_url_global_id_stays_within_limit(remote_links):
    url = "https://github.com/acme/web/compare/main..." + "x" * 300
    await call("add_remote_link", issue_key="ABC-7", url=url, title="Compare")
    sent = remote_links.sent_json("POST", "/issue/ABC-7/remotelink")[0]
    assert sent["globalId"].startswith("sha256=") and len(sent["globalId"]) <= 255
    assert sent["object"]["url"] == url


async def test_unknown_forge_gets_no_application(remote_links):
    await call("add_remote_link", issue_key="ABC-7", url="https://git.internal.acme.dev/web/commit/abc", title="Commit abc")
    assert "application" not in remote_links.sent_json("POST", "/issue/ABC-7/remotelink")[0]


@pytest.mark.parametrize("url", ["git@github.com:acme/web.git", "file:///etc/passwd", "javascript:alert(1)"])
async def test_remote_link_rejects_non_http_urls(jira, url):
    with pytest.raises(ToolError, match="not an http"):
        await call("add_remote_link", issue_key="ABC-7", url=url, title="x")
    assert jira.requests == []
