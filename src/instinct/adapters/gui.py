"""Native GUI lane via Cua Driver (last resort).

Cua Driver (github.com/trycua/cua, libs/cua-driver, Rust) drives macOS apps
without stealing focus: accessibility actions for standard controls, SkyLight
per-process event posting (SLEventPostToPid + focus-without-raise) for pixel
input, ScreenCaptureKit for occluded-window capture. We talk to it through its
documented agent interface, `cua-driver mcp` (MCP over stdio), kept open as one
session so element tokens stay valid between calls.

Background-only invariant, enforced in `CuaDriver.act()` for every action:
- `delivery_mode` is always forced to "background"; callers cannot override it.
- Desktop-scoped targets, modifier clicks, and foreground-only driver tools
  (bring_to_front, move_cursor, invoke_menu, zoom, drag, set_window_frame,
  launch_app, kill_app) are never exposed.
- If the driver refuses in the background, or says an action would need
  foreground delivery (`escalation.target == "foreground"`), we raise
  ForegroundRequired. There is no fallback.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from instinct.logs import redact

log = logging.getLogger("instinct.gui")

KNOWN_PATHS = [
    "~/.local/bin/cua-driver",
    "/Applications/CuaDriver.app/Contents/MacOS/cua-driver",
    "/opt/homebrew/bin/cua-driver",
    "/usr/local/bin/cua-driver",
]
# Driver tools this wrapper may call. Everything else is off-limits.
READ_TOOLS = {"list_apps", "list_windows", "get_window_state", "check_permissions"}
ACTION_TOOLS = {"click", "type_text", "press_key", "hotkey", "scroll", "set_value"}
BACKGROUND_REFUSALS = {"background_unavailable", "foreground_required"}
MODIFIER_KEYS = {"cmd", "command", "ctrl", "control", "alt", "option", "opt", "shift", "fn"}


class GuiError(RuntimeError):
    pass


class ForegroundRequired(GuiError):
    """The action can only be done in the foreground, which this lane never does."""


def find_driver(cfg) -> str | None:
    candidates = [cfg.gui.cua_driver_path] if cfg.gui.cua_driver_path else []
    if found := shutil.which("cua-driver"):
        candidates.append(found)
    candidates += KNOWN_PATHS
    for c in candidates:
        p = Path(os.path.expanduser(c))
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def doctor_check(cfg):
    from instinct.doctor import Check

    path = find_driver(cfg)
    fix = ('Install: /bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)" then grant CuaDriver.app '
           "Accessibility + Screen Recording (`cua-driver permissions grant`). Optional: only the gui_* tools "
           "and ask_claude(mode='desktop') need it.")
    if not path:
        return Check("Cua Driver", False, "cua-driver not found", fix)
    try:
        drv = CuaDriver(path)
        perms = drv.call("check_permissions", {})
        drv.close()
    except Exception as e:  # noqa: BLE001
        return Check("Cua Driver", False, f"{path} found but not reachable: {e}",
                     "Start it with `open -n -g -a CuaDriver --args serve`, then re-run doctor.")
    text = json.dumps(perms.data if perms.data is not None else perms.text).lower()
    missing = [p for p in ("accessibility", "screen") if re.search(rf'{p}[^,}}]*(false|denied|missing)', text)]
    if missing:
        return Check("Cua Driver", False, f"reachable, but CuaDriver.app lacks: {', '.join(missing)}",
                     "Run `cua-driver permissions grant` and enable CuaDriver in System Settings.")
    return Check("Cua Driver", True, f"reachable ({path})")


@dataclass
class ToolResult:
    data: Any
    text: str
    images: list[bytes] = field(default_factory=list)
    is_error: bool = False


def _parse_result(res) -> ToolResult:
    texts, images = [], []
    for block in res.content:
        t = getattr(block, "type", None)
        if t == "text":
            texts.append(block.text)
        elif t == "image":
            images.append(base64.b64decode(block.data))
    text = "\n".join(texts)
    data = getattr(res, "structured_content", None)
    if data is None:
        for t in texts:
            try:
                data = json.loads(t)
                break
            except json.JSONDecodeError:
                continue
    return ToolResult(data=data, text=text, images=images, is_error=bool(getattr(res, "is_error", False)))


def _error_code(r: ToolResult) -> str | None:
    if isinstance(r.data, dict):
        for k in ("code", "error_code", "refusal", "error"):
            v = r.data.get(k)
            if isinstance(v, str):
                return v
            if isinstance(v, dict) and isinstance(v.get("code"), str):
                return v["code"]
    m = re.search(r"\b([a-z]+(?:_[a-z]+)+)\b", r.text or "")
    return m.group(1) if (r.is_error and m) else None


class CuaDriver:
    """One long-lived `cua-driver mcp` session on a private event-loop thread."""

    def __init__(self, path: str, args: list[str] | None = None, timeout: float = 60.0,
                 env: dict[str, str] | None = None):
        from mcp import Client, StdioServerParameters

        self.timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="instinct-cua", daemon=True)
        self._thread.start()
        self._params = StdioServerParameters(command=path, args=args if args is not None else ["mcp"], env=env)
        self._client_cls = Client
        self._client = None
        self._stack = None
        self._lock = threading.Lock()
        self.snapshots: dict[int, dict] = {}  # pid -> last get_window_state summary
        self._run(self._open())
        atexit.register(self.close)

    def _run(self, coro, timeout: float | None = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout or self.timeout)
        except TimeoutError:
            fut.cancel()
            raise GuiError("Cua Driver did not respond in time") from None

    async def _open(self):
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(self._client_cls(self._params))
        tools = {t.name for t in (await self._client.list_tools()).tools}
        missing = (READ_TOOLS | ACTION_TOOLS) - tools
        if missing:
            log.warning("cua-driver lacks expected tools: %s", sorted(missing))
        self.tools = tools

    def close(self) -> None:
        if self._stack is None:
            return
        try:
            self._run(self._stack.aclose(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._stack = None
        self._loop.call_soon_threadsafe(self._loop.stop)

    def call(self, tool: str, args: dict) -> ToolResult:
        if tool not in READ_TOOLS | ACTION_TOOLS:
            raise GuiError(f"tool {tool!r} is not allowed in the background lane")
        with self._lock:
            res = self._run(self._client.call_tool(tool, args))
        return _parse_result(res)

    # ------------------------------------------------------------------ reads

    def list_windows(self, app: str | None = None) -> list[dict]:
        r = self.call("list_windows", {})
        if r.is_error:
            raise GuiError(r.text or "list_windows failed")
        wins = r.data.get("windows", r.data) if isinstance(r.data, dict) else r.data
        if not isinstance(wins, list):
            raise GuiError("unexpected list_windows output from cua-driver")
        if app:
            wins = [w for w in wins if app.lower() in (w.get("app_name") or "").lower()]
        return wins

    def resolve_window(self, app: str, window_id: int | None = None) -> dict:
        wins = self.list_windows(app)
        if window_id is not None:
            wins = [w for w in wins if w.get("window_id") == window_id]
        if not wins:
            raise GuiError(f"no window found for {app!r}. The app must already be running; this lane "
                           "never launches or activates apps.")
        exact = [w for w in wins if (w.get("app_name") or "").lower() == app.lower()] or wins
        # Prefer the app's front-most window that is on the current Space.
        exact.sort(key=lambda w: (bool(w.get("on_current_space", True)), w.get("z_index", 0)), reverse=True)
        return exact[0]

    def get_app_state(self, app: str, window_id: int | None = None, query: str | None = None,
                      max_elements: int = 300, include_screenshot: bool = False) -> dict:
        w = self.resolve_window(app, window_id)
        args = {"pid": w["pid"], "window_id": w["window_id"], "include_accessibility_tree": True,
                "include_screenshot": include_screenshot, "max_elements": max_elements}
        if query:
            args["query"] = query
        r = self.call("get_window_state", args)
        if r.is_error:
            raise GuiError(r.text or "get_window_state failed")
        d = r.data if isinstance(r.data, dict) else {}
        elements = d.get("elements") or []
        self.snapshots[w["pid"]] = {
            "window_id": w["window_id"], "snapshot_id": d.get("snapshot_id"),
            "labels": {e.get("element_token"): f"{e.get('role')} {e.get('label') or ''}".strip()
                       for e in elements if e.get("element_token")},
            "by_index": {e.get("element_index"): e.get("element_token") for e in elements},
        }
        return {
            "app": d.get("app_name") or w.get("app_name"),
            "pid": w["pid"],
            "window_id": w["window_id"],
            "window_title": d.get("window_title") or w.get("title"),
            "snapshot_id": d.get("snapshot_id"),
            "degraded": d.get("degraded", False),
            "off_space": d.get("off_space", False),
            "elements": [{k: e.get(k) for k in ("element_token", "element_index", "role", "label", "value",
                                                  "actions")} for e in elements],
            "tree_markdown": d.get("tree_markdown") or r.text,
            "screenshot": r.images[0] if r.images else None,
        }

    def screenshot_window(self, app: str, window_id: int | None = None) -> tuple[dict, bytes]:
        state = self.get_app_state(app, window_id, max_elements=1, include_screenshot=True)
        if not state["screenshot"]:
            raise GuiError("Cua Driver returned no screenshot; grant CuaDriver.app Screen Recording.")
        return {k: state[k] for k in ("app", "pid", "window_id", "window_title")}, state["screenshot"]

    # ------------------------------------------------------------------ actions

    def target(self, app: str, element_id: str | int | None, window_id: int | None = None) -> dict:
        w = self.resolve_window(app, window_id)
        args: dict[str, Any] = {"pid": w["pid"], "window_id": w["window_id"]}
        if element_id is None:
            return args
        snap = self.snapshots.get(w["pid"])
        eid = str(element_id).strip()
        if eid.isdigit():
            if not snap or not snap.get("snapshot_id"):
                raise GuiError("call gui_get_app_state first: numeric element ids need a snapshot")
            args.update(element_index=int(eid), snapshot_id=snap["snapshot_id"])
        else:
            args["element_token"] = eid
        return args

    def describe(self, app: str, element_id) -> str:
        for snap in self.snapshots.values():
            token = snap["by_index"].get(int(element_id)) if str(element_id).isdigit() else str(element_id)
            if token in snap["labels"]:
                return f"{snap['labels'][token]!r} ({element_id})"
        return str(element_id)

    def act(self, tool: str, args: dict) -> dict:
        """Run an action with the background-only invariant enforced."""
        if tool not in ACTION_TOOLS:
            raise GuiError(f"{tool!r} is not an allowed action")
        target = args.get("target")
        if isinstance(target, dict) and target.get("kind") == "desktop":
            raise ForegroundRequired("desktop-scoped actions are foreground-only; refused")
        if args.get("modifier"):
            raise ForegroundRequired("modifier clicks are foreground-only on macOS; refused")
        args = {**args, "delivery_mode": "background"}
        r = self.call(tool, args)
        code = _error_code(r)
        d = r.data if isinstance(r.data, dict) else {}
        escalation = d.get("escalation") or {}
        effect = d.get("effect")
        if code in BACKGROUND_REFUSALS or effect == "refused" or (
                isinstance(escalation, dict) and escalation.get("target") == "foreground"):
            raise ForegroundRequired(
                f"{tool} can't be done in the background here ({code or escalation.get('reason') or effect}). "
                "This lane never falls back to foreground input.")
        if code in ("stale_element_token", "snapshot_id_required"):
            raise GuiError("The element reference is stale. Call gui_get_app_state again and re-propose.")
        if r.is_error:
            raise GuiError(r.text or f"{tool} failed")
        log.info("gui %s pid=%s effect=%s route=%s", tool, args.get("pid"), effect, d.get("route"))
        return {"effect": effect, "route": d.get("route"), "delivery": d.get("delivery"),
                "escalation": escalation or None, "summary": r.text[:500] if r.text else None}

    def click(self, app: str, element_id, window_id: int | None = None) -> dict:
        return self.act("click", {**self.target(app, element_id, window_id), "action": "press"})

    def type_text(self, app: str, text: str, element_id=None, window_id: int | None = None) -> dict:
        return self.act("type_text", {**self.target(app, element_id, window_id), "text": text})

    def press_keys(self, app: str, keys: str | list[str], element_id=None, window_id: int | None = None) -> dict:
        parts = [k.strip().lower() for k in (keys if isinstance(keys, list) else re.split(r"\s*\+\s*", keys))
                 if k.strip()]
        if not parts:
            raise GuiError("no keys given")
        base = self.target(app, element_id, window_id)
        if len(parts) == 1:
            return self.act("press_key", {**base, "key": parts[0]})
        if parts[-1] in MODIFIER_KEYS:
            raise GuiError("a key combo must end with a non-modifier key, e.g. 'cmd+c'")
        return self.act("hotkey", {**base, "keys": parts})

    def scroll(self, app: str, direction: str, amount: int = 3, by: str = "line", element_id=None,
               window_id: int | None = None) -> dict:
        if direction not in ("up", "down", "left", "right"):
            raise GuiError("direction must be up, down, left or right")
        if by not in ("line", "page"):
            raise GuiError("by must be line or page")
        return self.act("scroll", {**self.target(app, element_id, window_id), "direction": direction,
                                   "amount": amount, "by": by})


_DRIVERS: dict[int, CuaDriver] = {}


def get_driver(cfg) -> CuaDriver:
    path = find_driver(cfg)
    if not path:
        raise GuiError('Cua Driver is not installed. Install it with /bin/bash -c "$(curl -fsSL '
                       'https://cua.ai/driver/install.sh)" and see `uv run instinct doctor`.')
    key = id(cfg)
    if key not in _DRIVERS:
        _DRIVERS[key] = CuaDriver(path)
    return _DRIVERS[key]


# --------------------------------------------------------------------------- Claude desktop (experimental)

COMPOSER_ROLES = ("AXTextArea", "AXTextField")
CLAUDE_APP = "Claude"


def ask_claude_desktop(driver: CuaDriver, prompt: str, timeout: float = 180.0, poll_s: float = 2.0) -> dict:
    """Type a prompt into the Claude desktop app in the background and read the reply.

    Experimental. Claude desktop is Electron, which pauses accessibility-tree
    updates while its window is occluded, so the reply may not become readable
    until the window is visible. We report that instead of raising the window.
    """
    state = driver.get_app_state(CLAUDE_APP, query=None, max_elements=400)
    boxes = [e for e in state["elements"] if e.get("role") in COMPOSER_ROLES]
    if not boxes:
        raise GuiError("Couldn't find the Claude desktop message box in the accessibility tree (the app may be "
                       "on another Space or occluded; Electron trees go stale when hidden).")
    box = boxes[-1]
    before = state["tree_markdown"] or ""
    driver.type_text(CLAUDE_APP, prompt, element_id=box["element_token"])
    driver.press_keys(CLAUDE_APP, "return", element_id=box["element_token"])
    log.info("claude desktop: sent %s", redact(prompt))

    deadline = time.monotonic() + timeout
    last, stable = None, 0
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        tree = driver.get_app_state(CLAUDE_APP, max_elements=400)["tree_markdown"] or ""
        if tree == last and tree != before:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = tree
    else:
        raise GuiError("No readable reply from the Claude desktop app before the timeout. Electron pauses "
                       "accessibility updates while the window is occluded; this is a known limitation.")
    new_text = last[len(before):] if last.startswith(before) else last
    return {"mode": "desktop", "experimental": True, "window_text_after_send": new_text[-6000:]}
