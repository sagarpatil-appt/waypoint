# Waypoint

An MCP (Model Context Protocol) server that routes a ticket through to shipped code. Today that
means Jira Cloud — search, read, create, and comment on tickets, then implement, commit, and push
the change — from inside Claude Desktop, Claude Code, or any other MCP-compatible assistant. The
name is deliberately not Jira-specific: the ticket-to-code loop it automates is meant to grow to
other trackers/platforms (e.g. GitHub Issues/PRs) later without a rebrand.

## Command-line install (recommended)

This project ships a `waypoint` console script (`pyproject.toml`'s `[project.scripts]`), so it can
be installed and run with a single command via [`uv`](https://docs.astral.sh/uv/) — no manual
cloning, no venv activation, no `pip install` step:

```bash
claude mcp add waypoint -- uvx --from git+https://github.com/sagarpatil-appt/waypoint waypoint
```

`uvx` builds an isolated environment for the package on first run and reuses it afterward — the
user never has to think about Python versions or dependencies. This has been tested end-to-end:
running it from a directory with no venv at all installs cleanly and the server starts without
error.

After adding it, reload/restart your MCP client (`/mcp` → reconnect, or reload the window) and ask
your assistant anything Jira-related — since no credentials are configured yet, it will ask you
for your site URL, email, and API token in the chat and save them for next time (see "First-time
setup" below).

### Updating

`uvx` caches the environment it builds, so it keeps running the version you first installed
rather than picking up new releases on its own. Ask your assistant to run `check_for_updates` to
see whether a newer release is out. To upgrade:

```bash
uv cache clean waypoint
```

Then reconnect the server in your MCP client (`/mcp` → reconnect, or restart the session). The
next launch rebuilds Waypoint from the latest commit on GitHub. Your saved Jira connection and
`set_project_workspace` mappings are kept — they live in your user config directory (see
"First-time setup" below), not in uv's cache.

**Upgrading from 0.2.0 or earlier with `uvx`?** Those versions saved credentials inside uv's
cache, so the cache clean removes them and you'll be asked to connect once more. Keep your API
token handy, or create a new one at
[id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens).

If the server still fails to start after upgrading — for example with
`ModuleNotFoundError: No module named 'mcp.server.fastmcp'` from a pre-0.2.0 environment — clear
uv's whole cache and reconnect again:

```bash
uv cache clean
```

If `claude mcp list` still shows `waypoint` pointing at an old local path instead of the `uvx`
command, a project-scoped entry (e.g. in a `.mcp.json`) is overriding it. Remove that one:

```bash
claude mcp remove waypoint --scope project
```

### Manual install from source

If you'd rather clone and manage the venv yourself:

```bash
git clone https://github.com/sagarpatil-appt/waypoint
cd waypoint
uv sync
```

Then point your MCP client at `.venv/bin/python waypoint_server.py`, e.g. via a project-scoped
`.mcp.json`:

```json
{
  "mcpServers": {
    "waypoint": {
      "command": "/absolute/path/to/waypoint/.venv/bin/python",
      "args": ["/absolute/path/to/waypoint/waypoint_server.py"]
    }
  }
}
```

## One-click install for Claude Desktop (alternative, GUI-based)

This repo includes a [`manifest.json`](manifest.json) so it can be packaged as an **MCPB** (Claude
Desktop Extension) — a `.mcpb` bundle a user double-clicks to install, no `.mcp.json` editing or
chat back-and-forth required. Claude Desktop reads `manifest.json`'s `user_config` section and
generates a native install form asking for:

- **Jira site URL**
- **Atlassian account email**
- **Jira API token** — masked as you type and stored in the OS keychain (Keychain on macOS,
  Credential Manager on Windows), never written to disk in plain text

All three are collected in that one popup at install time; the server never has to ask for them
in chat, and there's no local `.env` file to create.

To package it:

```bash
npm install -g @anthropic-ai/mcpb
# vendor this project's Python dependencies into lib/, since (unlike Node) Claude Desktop
# does not bundle a Python runtime or its own site-packages for you:
pip install --target=lib "mcp[cli]>=2.2.0,<3.0.0" "httpx>=0.27.0" "python-dotenv>=1.1.0"
mcpb pack
```

This produces a `.mcpb` file. Installing it: double-click the file, drag it into the Claude
Desktop window, or use Settings → Extensions → Advanced settings → Install Extension…

Notes:
- Update the placeholder `author.name` in `manifest.json` before distributing.
- `manifest.json` currently declares `"platforms": ["darwin"]` — it hasn't been tested on Windows;
  the `command` there (`python3`) is also macOS/Linux-specific and would need to be `python` on
  Windows.
- The server itself doesn't need any code changes for this path: it reads `JIRA_SITE_URL`,
  `JIRA_EMAIL`, `JIRA_API_TOKEN` from the environment the same way whether they come from a local
  `.env` file or from Claude Desktop injecting `user_config` values as env vars at launch.

## First-time setup via chat (command-line / manual-install path)

If you installed via the CLI (above) rather than the one-click `.mcpb`, you don't need to create a
`.env` file yourself. The first time you ask your assistant to do anything Jira-related,
it will notice no connection is configured yet and ask you for three things:

1. **Jira site URL** — e.g. `https://yourcompany.atlassian.net`
2. **Atlassian account email** — the email you sign in to Jira with
3. **API token** — create one at
   [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)

Just answer with those three values when asked. The assistant validates them against Jira before
saving, and stores them for future sessions in `~/.config/waypoint/.env` (`%APPDATA%\waypoint\.env`
on Windows; `$XDG_CONFIG_HOME/waypoint/.env` if that's set), readable only by you. You won't be
asked again unless you reconnect to a different site or your API token expires — if Jira rejects
the saved token, tools say so and ask you to run setup again, rather than returning empty results.

Installed from source before 0.2.1? Your existing `.env` next to `waypoint_server.py` is copied to
the new location automatically on first run; you can delete the old one afterwards.

## Quick start

New to this server? Ask your assistant to run the **`tour`** prompt — it checks your connection,
explains what this server does, shows your actual open tickets, and suggests a concrete next step
instead of a generic example.

## What you can ask it to do

Once connected, you can talk to your assistant in plain language, for example:

- "What Jira tickets are assigned to me right now?"
- "Show me the details of ABC-123, including comments and attachments."
- "Create a bug ticket in project ABC titled '...'."
- "Add a comment to ABC-123 saying ..."
- "Read ABC-123, check if the requirement is clear, and if there are gaps, comment on the ticket
  and break it into sub-tasks."
- "Implement ABC-123" — runs the full `implement_ticket` workflow below.

## The core workflow this is built for

This server is designed around one loop: **a developer working one ticket at a time, in their
editor.** Ask your assistant to "implement ABC-123" (or invoke the `implement_ticket` prompt
directly) and it will:

1. Read the ticket in full — description, comments, acceptance criteria and other custom fields,
   parent/linked issues, and any screenshots or logs attached to it (`get_ticket`,
   `download_attachment`)
2. Verify it's actually in the right repo/workspace for that ticket's project — checking a saved
   `set_project_workspace` mapping first, or falling back to the git remote/directory name and
   asking you to confirm if it can't tell — before touching anything
3. Decide if the requirement is actually clear enough to act on
4. If not: post a comment explaining the specific gap (`add_comment`) and stop — no guessing
5. If it is: mark it as the working issue, move its status to in-progress, analyze the existing
   code, then write the actual code in your project using its normal file/editing tools (not a
   Jira tool — Jira has no idea how to write code; this step happens entirely outside this server)
6. Run your tests/build/lint — if they still fail after a reasonable fix attempt, it stops there
   and reports the failure instead of committing broken code
7. Comment a summary back and log the time spent (`add_worklog`, auto-computed from real elapsed
   time by default), and move the ticket to a review-style status rather than jumping to "Done"
8. Ask you whether to commit directly or on a new branch (matching this repo's existing naming
   convention), stage only the files it actually touched, and commit as `TICKET-KEY: <summary>`
9. Ask again before pushing — pushing always needs a separate yes, even if you already said
   "commit directly" — and offer to open a PR/MR if a new branch was pushed

This is deliberately not trying to be a full Jira administration tool — no sprints, boards, or
epics here. If you need broad Jira/Confluence coverage, Atlassian's own
[Rovo MCP Server](https://github.com/atlassian/atlassian-mcp-server) already covers that. This
project stays scoped to the ticket-to-code loop above.

## Available tools

| Tool | Purpose |
|---|---|
| `jira_connection_status` | Check whether a Jira connection is currently configured |
| `setup_jira_connection` | Validate and save Jira site URL, email, and API token |
| `check_for_updates` | Check whether a newer Waypoint release is available on GitHub |
| `search_tickets` | Search tickets with a raw JQL query (pages through results and returns `has_more` plus Jira's estimated `total`, so a capped result set is visible) |
| `my_open_tickets` | List the current user's open (not Done) tickets |
| `get_ticket` | Read a ticket's full details as Markdown — description, comments, custom fields (acceptance criteria, story points, sprint), parent/sub-tasks, linked issues, web links, and attachments |
| `download_attachment` | Download an attachment to a local file so it can be read; images (screenshots) are also shown to the model directly |
| `list_issue_types` | List every issue type in a project, flagged sub-task or not — check this before create_ticket/create_subtask if the exact name isn't known |
| `create_ticket` | Create a new ticket in a project — validates `issue_type` against the project's actual types rather than guessing |
| `create_subtask` | Create a sub-task under an existing ticket — requires `issue_type` if the project has more than one sub-task type, rather than guessing |
| `add_comment` | Add a comment to an existing ticket, in plain developer language, with any relevant screenshot attached — Markdown (code blocks, lists, links) renders properly on the ticket |
| `get_available_transitions` | List the status transitions actually available for a ticket (status names are workflow-specific — check this before guessing) |
| `update_ticket_status` | Move a ticket to a new status — accepts the status name ('In Progress') or the transition name ('Start Progress'), and a resolution when the transition requires one |
| `update_ticket_assignee` | Reassign (or unassign) a ticket — accepts an email, display name, or accountId (accountId works even on sites that restrict user search) |
| `add_worklog` | Log time spent on a ticket; leave `time_spent` empty to auto-log real elapsed time since `set_working_issue` |
| `list_projects` | List Jira projects visible to the user, to find a valid project key |
| `set_project_workspace` / `get_project_workspace` | Remember which local repo (and subdirectory) a Jira project maps to, so `implement_ticket` can verify the workspace without asking every time |
| `add_attachment` | Attach a local file to a ticket |
| `add_watcher` / `list_watchers` | Add or list watchers on a ticket (accepts/returns accountId too) |
| `list_link_types` / `link_tickets` | List valid link types (blocks, relates to, etc.) and link two tickets together |
| `list_favorite_filters` | List the user's saved Jira filters, with JQL to run via `search_tickets` |
| `set_working_issue` / `get_working_issue` | Track a "current" ticket for the session so its key doesn't need repeating |
