import pytest

from conftest import adf_errors
from waypoint_server import _adf_from_markdown, _text_from_adf

DEV_SUMMARY = """## What changed

Fixed the **null pointer** in `AuthService.refresh()` — it ran before the token loaded.

- Added a guard in `src/auth/service.ts`
- Updated tests:
  - `refresh.test.ts` covers the expired-token case
  - removed a flaky *sleep*-based test
- See [PR #42](https://github.com/acme/app/pull/42)

1. Pull main
2. Run `npm test`

```ts
if (!token) {
  return null;
}
```

> Note: the old behaviour is still behind a **flag**.

---

Logs: https://ci.example.com/run/123."""

MARKDOWN_CASES = {
    "plain": "Fixed the null check in the login flow.",
    "empty": "",
    "multiline paragraph": "line one\nline two\nline three",
    "dev summary": DEV_SUMMARY,
    "snake_case and dunders": "Renamed my_var_name and touched __init__ in utils_helper.py, x * y * z.",
    "ordered list starting at 3": "3. third\n4. fourth",
    "code inside a link, link inside bold": "[`code link`](https://example.com) and **bold [link](https://a.b)**",
    "unclosed fence": "```python\nprint('hi')",
    "empty fence": "```\n```",
    "heading inside quote": "> # Heading in quote\n> text",
    "list then paragraph": "- a\n- b\n\nAfter list.",
    "mixed list types": "- bullet\n1. numbered",
    "deep nesting": "- a\n  - b\n    - c\n- d",
    "bare url": "https://jira.example.com/browse/ABC-1",
    "windows newlines": "a\r\nb\r\n\r\n- c",
    "dashes under a paragraph": "para\n---\nnext",
    "stars only": "***",
    "unicode": "Café ✅ — “quotes” 🚀",
}


@pytest.mark.parametrize("markdown", MARKDOWN_CASES.values(), ids=MARKDOWN_CASES.keys())
def test_markdown_converts_to_schema_valid_adf(markdown, adf_validator):
    assert adf_errors(adf_validator, _adf_from_markdown(markdown)) == []


def test_dev_summary_round_trips_unchanged():
    assert _text_from_adf(_adf_from_markdown(DEV_SUMMARY)) == DEV_SUMMARY


def test_dev_summary_produces_real_structure():
    blocks = [b["type"] for b in _adf_from_markdown(DEV_SUMMARY)["content"]]
    assert blocks == [
        "heading", "paragraph", "bulletList", "orderedList", "codeBlock", "blockquote", "rule", "paragraph",
    ]


def test_identifiers_are_not_mistaken_for_emphasis():
    text = MARKDOWN_CASES["snake_case and dunders"]
    paragraph = _adf_from_markdown(text)["content"][0]
    assert all("marks" not in node for node in paragraph["content"])
    assert _text_from_adf(_adf_from_markdown(text)) == text


def test_inline_marks():
    nodes = _adf_from_markdown("**b** *i* ~~s~~ `c` [l](https://x.y)")["content"][0]["content"]
    marked = {n["text"]: [m["type"] for m in n.get("marks", [])] for n in nodes if n["type"] == "text"}
    assert marked["b"] == ["strong"] and marked["i"] == ["em"] and marked["s"] == ["strike"]
    assert marked["c"] == ["code"] and marked["l"] == ["link"]


def test_code_mark_only_combines_with_link():
    nodes = _adf_from_markdown("**bold `code` bold**")["content"][0]["content"]
    code = next(n for n in nodes if n["text"] == "code")
    assert [m["type"] for m in code["marks"]] == ["code"]


def test_nested_list_structure():
    doc = _adf_from_markdown("- a\n  - b\n- c")
    outer = doc["content"][0]
    assert outer["type"] == "bulletList" and len(outer["content"]) == 2
    assert outer["content"][0]["content"][1]["type"] == "bulletList"


def test_ordered_list_keeps_start_number():
    assert _adf_from_markdown("3. x\n4. y")["content"][0]["attrs"] == {"order": 3}


def test_fenced_code_keeps_language_and_content():
    block = _adf_from_markdown("```python\nprint('hi')\n```")["content"][0]
    assert block == {"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "print('hi')"}]}


def P(*content):
    return {"type": "paragraph", "content": list(content)}


def T(text, *marks):
    return {"type": "text", "text": text, **({"marks": list(marks)} if marks else {})}


def doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def test_reads_rich_jira_content_as_markdown():
    description = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [T("Steps")]},
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [P(T("Open "), T("/login", {"type": "code"}))]},
            {"type": "listItem", "content": [P(T("Click submit")), {"type": "bulletList", "content": [
                {"type": "listItem", "content": [P(T("nested detail"))]}]}]}]},
        {"type": "codeBlock", "attrs": {"language": "json"}, "content": [T('{"error": 500}')]},
        {"type": "panel", "attrs": {"panelType": "warning"}, "content": [P(T("Prod only"))]},
        {"type": "table", "content": [
            {"type": "tableRow", "content": [{"type": "tableHeader", "content": [P(T("Browser"))]}, {"type": "tableHeader", "content": [P(T("Result"))]}]},
            {"type": "tableRow", "content": [{"type": "tableCell", "content": [P(T("Safari"))]}, {"type": "tableCell", "content": [P(T("fails | 500"))]}]}]},
        {"type": "taskList", "content": [
            {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [T("repro")]},
            {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [T("fix")]}]},
        P(T("See "), T("the spec", {"type": "link", "attrs": {"href": "https://acme.atlassian.net/wiki/x"}}),
          T(" — "), {"type": "status", "attrs": {"text": "BLOCKED"}}, T(" since "),
          {"type": "date", "attrs": {"timestamp": "1767225600000"}}),
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "m1", "alt": "error.png"}}]},
        {"type": "expand", "attrs": {"title": "Logs"}, "content": [P(T("stack trace"))]},
        {"type": "someFutureNode"},
    )
    assert _text_from_adf(description) == "\n\n".join([
        "## Steps",
        "1. Open `/login`\n2. Click submit\n   - nested detail",
        '```json\n{"error": 500}\n```',
        "> **Warning:** Prod only",
        "| Browser | Result |\n|---|---|\n| Safari | fails \\| 500 |",
        "- [x] repro\n- [ ] fix",
        "See [the spec](https://acme.atlassian.net/wiki/x) — [BLOCKED] since 2026-01-01",
        "[attachment: error.png]",
        "**Logs**\n\nstack trace",
        "[someFutureNode]",
    ])


def test_mention_only_comment_is_not_blank():
    mentions = doc(P({"type": "mention", "attrs": {"id": "u1", "text": "@Priya"}}, {"type": "mention", "attrs": {"id": "u2"}}))
    assert _text_from_adf(mentions) == "@Priya@u2"


@pytest.mark.parametrize("value", [None, "", {}, "plain string passes through"])
def test_degenerate_inputs(value):
    assert _text_from_adf(value) == (value if isinstance(value, str) else "")
