# Instinct

Local macOS toolkit that lets an AI agent read and send your texts, check Canvas,
and ask Claude, **without touching what you're doing**: no cursor movement, no focus
stealing, no raised windows, no Space switches. Everything runs on your Mac and
is exposed as tools over MCP, so Claude Code, Claude Desktop, Cursor, or your
own agent loop can use it. The **iMessage bridge** extends this to an assistant you
text, such as Instinct AI: it sends `@mac …` in your thread, and your Mac does the work
and texts back.

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
| Contacts (optional) | Showing names instead of phone numbers | Covered by Full Disk Access (reads the local AddressBook DB); or `uv run instinct contacts-auth` |
| Automation → Messages | `messages_send` and the bridge | macOS asks the first time a text is sent |
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

## Text your Mac: the iMessage bridge

Assistants that live in Messages, like **Instinct AI**, can't call a local MCP server.
They can send a text, though. `instinct bridge` watches the thread you have with the
assistant. When a message starts with `@mac`, it runs the request on this Mac with
Claude Code (your `claude` login, with only Instinct's tools: no shell, files or web)
and texts the answer back into the same thread.

```
Instinct:  @mac what did Sam last say about Saturday?
you (Mac): 🖥️ Sam, Wed 9:14 PM: "down for sat, will confirm time". Nothing since.
Instinct:  @mac text Sam "what time works saturday?"
you (Mac): 🖥️ Approval needed: Text Sam Lee (+1555…) from your Messages account:
           what time works saturday?
           Reply "ok 4821" to allow or "no 4821" to cancel (expires in 10 min).
you:       ok 4821
you (Mac): 🖥️ Sent.
```

Setup:

1. In `~/.instinct/config.toml`, add the assistant's number:

   ```toml
   [bridge]
   chats = ["+15555550123"]   # the number you text your assistant at
   ```

2. Run it: `uv run instinct bridge` (keep the terminal open), or
   `uv run instinct bridge install` to run it as a login agent. The install
   command prints the Python binary that needs Full Disk Access, because launchd
   doesn't inherit your terminal's permissions.
3. Tell the assistant once, for example: *"You can now use my Mac. When you need my
   texts, Canvas, Claude, or an app, send a message that starts with `@mac` and
   then the request in plain English. My Mac replies in this thread with a 🖥️."*
   You can also type `@mac …` yourself.

How it's kept safe:

- Only the configured chats are watched, and only messages that start with the
  trigger count. History isn't replayed, the bridge ignores its own 🖥️ messages,
  and it runs at most 20 tasks an hour (`max_tasks_per_hour`).
- **Reads run without asking, and their results leave your Mac.** A reply goes to
  the assistant's servers like any text you send it. Only point the bridge at an
  assistant you'd paste that information into anyway.
- **Every side effect needs you.** Sending a text, clicking in an app, or sending
  to claude.ai goes through the same `confirm_action` gate. The bridge texts you
  the gate's summary and a random 4-digit code. The action runs only when *you*
  reply exactly `ok <code>`. The assistant can't approve anything, because its
  messages aren't from you, and a request that goes unanswered for 10 minutes is
  cancelled.
- Follow-ups within 30 minutes continue the same Claude session, so "@mac now
  text her that" works.

## Tools

| Tool | Kind |
|---|---|
| `messages_list_chats(query?, limit)` · `messages_read_thread(chat, since?, limit)` · `messages_search(text, chat?, since?, limit)` · `messages_whats_new(mark_seen)` | read |
| `messages_send(to, text)` | **gated** |
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
- **Read-only data.** chat.db and its `-wal`/`-shm` files are cloned (APFS
  copy-on-write) into a private temp dir and opened with `mode=ro`. The live
  database is never opened. Sending goes through Messages' own AppleScript
  `send` command, with the text passed as an argument rather than spliced into
  the script, and only after `confirm_action`.
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
`imessage_send.py`, `browser.py`, `gui.py`, `claude.py`, `bridge.py`, `router.py`, `safety.py`, `selectors.py`,
`config.py`, `server.py`, `doctor.py`, `cli.py`), `tests/`, `scripts/`.
See [NOTES.md](NOTES.md) for what's verified, what's flaky, and what depends on
private macOS APIs.
