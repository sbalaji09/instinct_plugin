# NOTES

Status as of 2026-09-18, on macOS 26.2 (25C56), Apple Silicon, Python 3.12, MCP SDK 2.2.0.

## Verified working

| Area | How it was verified |
|---|---|
| attributedBody decoder | 17 tests on genuine typedstream blobs made by the system `NSArchiver` (`scripts/make_fixtures.py`): 1/2/4-byte length prefixes (127/128/70000 bytes), emoji and flags, CJK, embedded NUL, attachments (U+FFFC), and `NSMutableAttributedString` with several attribute runs. |
| Messages adapter | Synthetic chat.db with the real schema subset, in WAL mode with rows left **only in the -wal file**. Covers seconds and nanosecond timestamps, tapbacks, group events skipped, contact resolution, cursor paging, and a guard that fails if the live DB path is ever opened. |
| Canvas adapter | Mocked HTTP (respx): Link-header pagination, 429 and legacy 403 throttling, Retry-After, low-quota slowdown, 401 fast-fail, login-redirect detection, and course resolution by id, code, or name. |
| MCP server | End-to-end through the SDK client, both in-process and by spawning the real `instinct-mcp` over stdio. Checks that errors come back as `is_error` and that logs contain no message bodies. |
| Safety gate | Every tool without `read_only_hint` is enumerated and called, and the test asserts nothing reached the driver or browser before `confirm_action`. Also covers single-use ids, expiry, cancel, and envelope escape attempts. |
| Browser lane | **Real headless Chrome** over CDP against a local fake claude.ai page: types into the contenteditable box, clicks Send, waits for streaming to end, reads the reply. Also tested: the Canvas cookie transport (`while(1);` prefix plus pagination) and clean Chrome shutdown. |
| `ask_claude` cli | **Live** call to `claude -p` (Claude Code 2.1.277) returned "pong". |
| `ask_claude` api | Request shape only (fake client). No `ANTHROPIC_API_KEY` in the build environment. |
| GUI lane | Against a fake `cua-driver mcp` server with the documented tool names and shapes: `delivery_mode` forced to background, foreground escalation and `background_unavailable` raise without retrying, forbidden tools are blocked, key combos map to `hotkey`, and screenshots work. |

## Not yet verified on real data

These need permissions or logins the build environment didn't have. Run
`uv run instinct doctor` first.

- **chat.db on this Mac.** The shell had no Full Disk Access. The schema columns
  used (`message.attributedBody`, `associated_message_type`, `item_type`,
  `cache_has_attachments`, `chat_message_join`, `chat_handle_join`) have been
  stable since macOS 11. If Apple renames any, the queries in `messages.py` are
  the place to fix.
- **Canvas (canvas.calpoly.edu).** No token was configured.
- **claude.ai web mode.** The profile isn't logged in yet. The selectors in
  `src/instinct/selectors.py` use role/ARIA first, but they were not checked
  against the live site. After `instinct login claude`, run
  `uv run instinct check-web` to see which candidates match.
- **Cua Driver.** Not installed (its installer is `curl | bash` into
  /Applications, so it was left for you to run). The wrapper follows the
  documented 0.28.x MCP interface but has only run against a fake driver.
- **Apps tested with Cua Driver:** none yet. Once it's installed, try Notes or
  Finder first (native AppKit, accessibility-friendly), then Claude desktop.

## Expected to be flaky

- **claude.ai selectors.** The DOM changes without notice. Completion is
  detected three ways (a `data-is-streaming` marker, the Stop button going
  away, and the text staying the same for 2 seconds), so a single changed
  selector usually degrades rather than breaks.
- **Headless Chrome on claude.ai.** Cloudflare / bot protection may challenge
  headless sessions even with the normalized user agent. If it does, set
  `[browser].headless = false`. Chrome then runs headed but is launched with
  `open -g`, and tabs are opened with CDP `background: true`.
- **Claude desktop (`mode="desktop"`).** Electron stops updating its
  accessibility tree while the window is covered. The reply may not become
  readable until the window is visible; the tool times out with that
  explanation rather than raising the window. It returns raw window text, not
  a cleanly parsed reply.
- **Electron apps generally.** Per the Cua Driver docs: `type_text` via AX
  returns `unverifiable`, and scroll or pixel drag is refused in the
  background. Those refusals become `ForegroundRequired` errors.
- **Contacts matching** uses the last 10 digits of a phone number, which fits
  US numbers. International numbers with different national lengths may not
  resolve and fall back to the raw handle.
- **Router intent parsing** is keyword and regex based. `route` is advisory;
  agents can always call tools directly.

## Changes from the spec (docs won)

- **MCP SDK 2.x:** `FastMCP` is now `mcp.server.mcpserver.MCPServer`.
- **Canvas:** there is no `/api/v1/courses/:id/announcements` endpoint.
  Announcements use `GET /api/v1/announcements?context_codes[]=course_N`.
  Throttling is `429` (older deployments sent `403 Rate Limit Exceeded`), so
  both are handled.
- **Canvas fallback:** instead of scraping the dashboard HTML, the browser
  fallback calls the same REST API with the profile's session cookie (as
  Canvas' own web UI does). That is sturdier than scraping.
  `backend = "auto"` picks the token when present, otherwise the browser.
- **Cua Driver:**
  - It's a Rust project now (`libs/cua-driver`, v0.28.x, pre-release).
  - Window capture uses **ScreenCaptureKit**, not CGWindowList.
  - Tool names are `get_window_state` (not `get_app_state`), `press_key` and
    `hotkey`, and `element_token`s tied to snapshots.
  - Instinct's tool names follow the spec with a `gui_` prefix and map onto these.
  - Permissions belong to CuaDriver.app, not to the MCP host.
- **Tool names are namespaced** (`messages_search`, `canvas_todo`, `gui_click`…)
  because bare `search`/`click` are ambiguous in a 25-tool server.
- **Layout** uses `src/instinct/` (the uv packaging default) instead of top-level `instinct/`.

## Private or fragile macOS dependencies

| Dependency | Risk |
|---|---|
| **chat.db schema** (private, undocumented) | Apple can change it in any OS update. |
| **attributedBody typedstream** (NSArchiver format, deprecated but stable for decades) | Low risk. If Apple switches to keyed archives, the decoder will raise `TypedStreamError` and the text will show as `None`, not garbage. |
| **Cua Driver internals**: SkyLight `SLEventPostToPid` / `SLPSPostEventRecordTo` (private framework, the "focus-without-raise" recipe from yabai) | The most likely thing to break on a macOS update. The AX-action path uses public APIs and is sturdier. |
| **CGEventPostToPid, ScreenCaptureKit, Accessibility, Contacts** | Public APIs; low risk. |
| **AddressBook-v22.abcddb** (private Core Data schema), the fallback for contact names | Medium risk; if it changes, names fall back to raw handles. |
| **Chrome's cookie DB location** (`Default/Network/Cookies`), used only by `doctor` and the router to detect logins | Low risk; both old and new paths are checked. |

## Security notes

- The background Chrome listens for CDP on `127.0.0.1:<cdp_port>`. Any local
  process running as you could drive that logged-in profile while it's up. The
  server closes Chrome when it exits normally; after a crash, quit it yourself
  (`pkill -f instinct/chrome-profile`).
- The confirmation gate is a consent step, not a sandbox. The MCP client is
  what calls `confirm_action`, so keep that tool on manual approval.
