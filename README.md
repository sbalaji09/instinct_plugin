# Instinct

Local macOS toolkit that lets an AI agent read your texts, check Canvas, and ask
Claude, **without touching what you're doing**: no cursor movement, no focus
stealing, no raised windows, no Space switches. Everything runs on your Mac and
is exposed as tools over MCP, so Claude Code, Claude Desktop, Cursor, or your
own agent loop can use it.

## How it picks a path

Every capability prefers the lowest viable lane:

| # | Lane | Used for |
|---|------|----------|
| 1 | Official API / CLI | Canvas REST API, Anthropic API, `claude -p` |
| 2 | Local data on disk | Messages (`~/Library/Messages/chat.db`, read-only copy) |
| 3 | Background browser profile (CDP) | claude.ai (`mode="web"`), Canvas if tokens are disabled |
| 4 | Background native GUI (Cua Driver) | Any other app; Claude desktop (`mode="desktop"`, experimental) |

The `route` tool shows which lane a request would use, and why.

## Setup

Requirements: macOS 14+, [uv](https://docs.astral.sh/uv/), Google Chrome.
Python 3.12+ is managed by uv.

```sh
git clone <this repo> instinct && cd instinct
uv sync
uv run playwright install chromium   # only needed if you don't have Google Chrome
cp config.example.toml ~/.instinct/config.toml   # then edit base_url etc.
uv run instinct doctor
```

`instinct doctor` checks every permission and dependency and prints the exact fix
for anything missing. You can also run it as `uv run scripts/doctor.py`.

### Permissions

macOS grants privacy permissions to the **app that launches the server**: your
terminal when you use `uv run`, or Claude Desktop / Cursor when they spawn it.
Grant them to that app, then quit and reopen it.

| Permission | Needed for | Where |
|---|---|---|
| Full Disk Access | Messages (reading chat.db) | Privacy & Security → Full Disk Access |
| Contacts (optional) | Showing names instead of phone numbers | `uv run instinct contacts-auth` |
| Accessibility + Screen Recording | GUI lane only | Grant to **CuaDriver.app** (`cua-driver permissions grant`) |

### Canvas

1. In Canvas, go to Account → Settings → **+ New Access Token**.
2. Run `uv run instinct set-canvas-token` and paste the token. It is stored in the
   macOS Keychain (service `instinct`), never in a file. You can also set
   `INSTINCT_CANVAS_TOKEN`.
3. Set `[canvas].base_url` (e.g. `https://canvas.calpoly.edu`).

If your school disables personal tokens, the default `backend = "auto"` falls
back to the logged-in background browser profile (`uv run instinct login canvas`).
That path makes the same API calls with your session cookie.

### Claude

- `mode="api"` (default): set `ANTHROPIC_API_KEY`, or sign in with `ant auth login`.
  Uses `claude-opus-5` with server-side refusal fallback.
- `mode="cli"`: uses your `claude` CLI login. Runs `claude -p` with no tools and no
  MCP servers, in an empty temp directory.
- `mode="web"`: sign in once with `uv run instinct login claude`, then check the
  selectors with `uv run instinct check-web`.
- `mode="desktop"`: needs Cua Driver and the Claude desktop app running (experimental).

### Background browser profile

`~/.instinct/chrome-profile` is a separate Chrome instance from your normal
browser. `uv run instinct login <claude|canvas|URL>` opens it visibly **once**
so you can sign in; quit it with ⌘Q when you're done. After that the server
runs it headless (or headed but never activated, if you set
`[browser].headless = false`) and drives it over CDP on `127.0.0.1:9333`.

### Cua Driver (optional, GUI lane)

```sh
/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"
cua-driver permissions grant        # toggle CuaDriver on in System Settings
cua-driver telemetry disable        # optional; telemetry is on by default
uv run instinct doctor
```

## Register the server

**Claude Code**

```sh
claude mcp add instinct --scope user -- uv --directory /absolute/path/to/instinct run instinct-mcp
```

**Claude Desktop**: edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "instinct": {
      "command": "/Users/YOU/.local/bin/uv",
      "args": ["--directory", "/absolute/path/to/instinct", "run", "instinct-mcp"]
    }
  }
}
```

Then give **Claude.app** Full Disk Access and restart it. Any other MCP client
works the same way: stdio transport, command `uv --directory <repo> run instinct-mcp`.

**Keep `confirm_action` on manual approval.** Don't add
`mcp__instinct__confirm_action` to an allow-list or "always allow" it. That
per-call prompt is how *you* approve each side effect.

## Tools

| Tool | Kind |
|---|---|
| `messages_list_chats(query?, limit)` · `messages_read_thread(chat, since?, limit)` · `messages_search(text, chat?, since?, limit)` · `messages_whats_new(mark_seen)` | read |
| `canvas_upcoming(days)` · `canvas_todo()` · `canvas_course_assignments(course, include_past?)` · `canvas_announcements(since)` | read |
| `ask_claude(prompt, mode)` | read for api/cli; **gated** for web/desktop |
| `claude_web_send(prompt, conversation_url?)` | **gated** |
| `gui_list_windows(app?)` · `gui_get_app_state(app, query?)` · `gui_screenshot_window(app)` | read |
| `gui_click` · `gui_type_text` · `gui_press_keys` · `gui_scroll` | **gated** |
| `route(intent)` | explains which lane/tool to use |
| `confirm_action(action_id)` · `cancel_action` · `list_pending_actions` | safety gate |

`since` accepts ISO dates, `today`, `yesterday`, or relative values like `30m`, `6h`, `2d`, `1w`.

## Safety model

- **Untrusted content.** Everything from texts, Canvas, web pages, app UIs, and
  Claude replies comes back inside `<untrusted_content source=… id=…>`. The
  payload is JSON with `<`/`>` escaped, so it can't forge a closing tag. Tool
  descriptions tell the model never to follow instructions inside it.
- **Read vs act.** Side-effecting tools only *propose*. They return an
  `action_id` and a plain-language summary. Only `confirm_action(action_id)`
  executes the action. Ids are random, single-use, and expire after 10 minutes.
- **Read-only data.** chat.db and its `-wal`/`-shm` files are copied to a
  private temp dir and opened with `mode=ro`. The live database is never opened.
- **No secrets on disk or in logs.** Tokens live in env vars or the Keychain.
  `~/.instinct/instinct.log` redacts message bodies and queries unless you set
  `[safety].log_message_bodies = true`.
- **GUI lane is background-only.** `delivery_mode` is forced to `background`.
  Foreground-only driver tools are never exposed. If an action would need the
  foreground, it fails with an explicit error; there is no fallback.

## Development

```sh
uv run pytest            # ~100 tests; never touch your real chat.db or Canvas
uv run scripts/make_fixtures.py   # regenerate attributedBody fixtures via NSArchiver
```

The headless-Chrome integration test drives a local fake claude.ai page. Skip it
with `INSTINCT_SKIP_CHROME=1`.

Layout: `src/instinct/` (`adapters/messages.py`, `typedstream.py`, `canvas.py`,
`browser.py`, `gui.py`, `claude.py`, `router.py`, `safety.py`, `selectors.py`,
`config.py`, `server.py`, `doctor.py`, `cli.py`), `tests/`, `scripts/`.
See [NOTES.md](NOTES.md) for what's verified, what's flaky, and what depends on
private macOS APIs.
