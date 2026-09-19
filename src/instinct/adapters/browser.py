"""Browser lane: a dedicated Chrome profile driven over CDP, in the background.

- Profile: ~/.instinct/chrome-profile, fully separate from your normal Chrome
  (different --user-data-dir means a different Chrome instance).
- Headless by default. With headless=false, Chrome is launched through
  `open -g -n` (don't activate, new instance) and new tabs are created with
  CDP `Target.createTarget(background=true)`. Page.bringToFront is never used.
  Playwright input goes through CDP to the page, never through the OS cursor
  or keyboard.
- One-time login: `instinct login <site>` opens the profile visibly, without a
  debugging port, so you can sign in normally. Quit that window when done.

Playwright's async API runs on a private event-loop thread; the public methods
here are synchronous and thread-safe, so they work from MCP worker threads and
the CLI alike.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from instinct.logs import redact
from instinct.selectors import CLAUDE_WEB

log = logging.getLogger("instinct.browser")

CHROME_FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-features=CalculateNativeWinOcclusion",
]


class BrowserError(RuntimeError):
    pass


class NotLoggedIn(BrowserError):
    pass


def _app_bundle(chrome_path: str) -> str | None:
    m = re.match(r"(.+?\.app)/", chrome_path)
    return m.group(1) if m else None


def _chrome_major(chrome_path: str) -> str | None:
    try:
        out = subprocess.run([chrome_path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
    return m.group(1) if m else None


class _LoopThread:
    """A private asyncio loop on a daemon thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="instinct-browser", daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float | None = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise BrowserError(f"browser operation timed out after {timeout:.0f}s") from None


class BrowserLane:
    def __init__(self, cfg):
        self.cfg = cfg
        self.b = cfg.browser
        self.endpoint = f"http://127.0.0.1:{self.b.cdp_port}"
        self._rt: _LoopThread | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._claude_page = None
        self._launched_by_us = False
        self._lock = threading.Lock()  # one browser operation at a time

    # ------------------------------------------------------------------ lifecycle

    def _cdp_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.endpoint}/json/version", timeout=1) as r:
                return r.status == 200
        except OSError:
            return False

    def _launch(self) -> None:
        profile = self.b.profile_dir
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not Path(self.b.chrome_path).exists():
            raise BrowserError(f"Chrome not found at {self.b.chrome_path} ([browser].chrome_path)")
        args = [f"--user-data-dir={profile}", f"--remote-debugging-port={self.b.cdp_port}", *CHROME_FLAGS]
        if self.b.headless:
            args.append("--headless=new")
            # Headless Chrome advertises "HeadlessChrome" in its UA, which some sites
            # (claude.ai's bot protection included) treat differently. Present the
            # same UA the headed browser would.
            if major := _chrome_major(self.b.chrome_path):
                args.append("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
            log_file = open(self.cfg.home / "chrome.log", "ab")  # noqa: SIM115 (lives as long as Chrome)
            subprocess.Popen([self.b.chrome_path, *args, "about:blank"], stdout=log_file, stderr=log_file,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        else:
            app = _app_bundle(self.b.chrome_path)
            if not app:
                raise BrowserError("headed background mode needs chrome_path inside a .app bundle")
            # -g: do not bring to foreground. -n: new instance even if Chrome is running.
            subprocess.run(["open", "-g", "-n", "-a", app, "--args", *args, "about:blank"], check=True, timeout=15)
        self._launched_by_us = True
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._cdp_alive():
                log.info("background chrome up on %s (headless=%s)", self.endpoint, self.b.headless)
                return
            time.sleep(0.25)
        raise BrowserError(f"Chrome did not open its debugging port {self.b.cdp_port} within 30s. Is the "
                           "profile open in another window (e.g. `instinct login` still running)?")

    async def _connect(self):
        from playwright.async_api import async_playwright

        if self._browser is not None and self._browser.is_connected():
            return
        if self._pw is None:
            self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.endpoint)
        self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._claude_page = None

    def _ensure(self) -> None:
        self.cfg.ensure_home()
        if self._rt is None:
            self._rt = _LoopThread()
            atexit.register(self.stop)
        if not self._cdp_alive():
            self._launch()
        self._rt.run(self._connect(), timeout=30)

    def run(self, coro_factory, timeout: float = 60.0):
        with self._lock:
            self._ensure()
            return self._rt.run(coro_factory(), timeout=timeout)

    def stop(self) -> None:
        """Close the background Chrome if we started it."""
        if self._rt is None or self._browser is None:
            return

        async def _close():
            try:
                if self._launched_by_us:
                    cdp = await self._browser.new_browser_cdp_session()
                    await cdp.send("Browser.close")
                else:
                    await self._browser.close()
            except Exception:  # noqa: BLE001 (already gone)
                pass
            if self._pw:
                await self._pw.stop()

        try:
            self._rt.run(_close(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._pw = self._context = None
        if self._launched_by_us:
            deadline = time.monotonic() + 10
            while self._cdp_alive() and time.monotonic() < deadline:
                time.sleep(0.2)

    # ------------------------------------------------------------------ pages

    async def _new_background_page(self):
        """Open a tab without raising or activating anything."""
        if self.b.headless:
            return await self._context.new_page()
        cdp = await self._browser.new_browser_cdp_session()
        waiter = asyncio.ensure_future(self._context.wait_for_event("page", timeout=15000))
        await cdp.send("Target.createTarget", {"url": "about:blank", "background": True, "newWindow": False})
        return await waiter

    async def _first(self, page, candidates: list[str], timeout_ms: int = 0):
        """First visible locator among candidates, polling up to timeout_ms."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            for sel in candidates:
                loc = page.locator(sel)
                try:
                    n = await loc.count()
                    for i in range(min(n, 5)):
                        if await loc.nth(i).is_visible():
                            return loc.nth(i), sel
                except Exception:  # noqa: BLE001 (bad selector for this page state)
                    continue
            if time.monotonic() >= deadline:
                return None, None
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------ canvas over cookies

    def fetch(self, url: str, params=None) -> "BrowserResponse":
        async def _get():
            full = url
            if params:
                full += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
            r = await self._context.request.get(full, headers={"Accept": "application/json"}, max_redirects=0)
            return BrowserResponse(r.status, dict(r.headers), await r.text())

        return self.run(_get, timeout=60)

    # ------------------------------------------------------------------ claude.ai

    def claude_send(self, prompt: str, conversation_url: str | None = None) -> dict:
        timeout = self.b.response_timeout_s
        return self.run(lambda: self._claude_send(prompt, conversation_url, timeout), timeout=timeout + 60)

    async def _claude_page_get(self):
        if self._claude_page is None or self._claude_page.is_closed():
            self._claude_page = await self._new_background_page()
        return self._claude_page

    async def _claude_send(self, prompt: str, conversation_url: str | None, timeout: float) -> dict:
        S = CLAUDE_WEB
        page = await self._claude_page_get()
        target = conversation_url or S["new_chat_url"]
        origin = re.match(r"https?://[^/]+/", S["new_chat_url"]).group(0)
        if not target.startswith(origin):
            raise BrowserError(f"conversation_url must be a {origin} URL")
        await page.goto(target, wait_until="domcontentloaded", timeout=45000)
        if any(m in page.url for m in S["login_url_markers"]):
            raise NotLoggedIn("The background profile isn't signed in to claude.ai. Run "
                              "`uv run instinct login claude` once.")
        composer, csel = await self._first(page, S["composer"], timeout_ms=20000)
        if composer is None:
            if any(m in page.url for m in S["login_url_markers"]):
                raise NotLoggedIn("Signed out of claude.ai; run `uv run instinct login claude`.")
            raise BrowserError("Couldn't find the claude.ai message box. The page layout may have changed; update "
                               "src/instinct/selectors.py (see `uv run instinct check-web`).")
        before = await self._assistant_count(page)
        await composer.click()
        await page.keyboard.insert_text(prompt)
        send, _ = await self._first(page, S["send_button"], timeout_ms=3000)
        if send is not None and await send.is_enabled():
            await send.click()
        else:
            await page.keyboard.press("Enter")
        log.info("claude_web: sent prompt %s via %s", redact(prompt), csel)

        deadline = time.monotonic() + timeout
        while await self._assistant_count(page) <= before:
            if time.monotonic() > deadline:
                raise BrowserError("claude.ai didn't start a reply before the timeout")
            await asyncio.sleep(0.5)

        last, stable_since = None, time.monotonic()
        while True:
            text = await self._last_assistant_text(page)
            streaming = await self._is_streaming(page)
            if text != last:
                last, stable_since = text, time.monotonic()
            elif not streaming and text and time.monotonic() - stable_since >= 2.0:
                break
            if time.monotonic() > deadline:
                log.warning("claude_web: timed out while streaming; returning partial reply")
                return {"text": last, "partial": True, "conversation_url": page.url}
            await asyncio.sleep(0.5)
        return {"text": last, "partial": False, "conversation_url": page.url}

    async def _assistant_count(self, page) -> int:
        for sel in CLAUDE_WEB["assistant_message"]:
            try:
                n = await page.locator(sel).count()
            except Exception:  # noqa: BLE001
                continue
            if n:
                return n
        return 0

    async def _last_assistant_text(self, page) -> str | None:
        for sel in CLAUDE_WEB["assistant_message"]:
            loc = page.locator(sel)
            try:
                if await loc.count():
                    return (await loc.last.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _is_streaming(self, page) -> bool:
        for sel in CLAUDE_WEB["streaming_marker"]:
            try:
                if await page.locator(sel).count():
                    return True
            except Exception:  # noqa: BLE001
                pass
        stop, _ = await self._first(page, CLAUDE_WEB["stop_button"])
        return stop is not None

    def check_selectors(self) -> dict:
        """Report which claude.ai selectors match on the live page (diagnostics)."""

        async def _check():
            page = await self._claude_page_get()
            await page.goto(CLAUDE_WEB["new_chat_url"], wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)
            report: dict[str, Any] = {"url": page.url}
            for key in ("composer", "send_button", "stop_button", "assistant_message", "streaming_marker"):
                report[key] = {}
                for sel in CLAUDE_WEB[key]:
                    try:
                        report[key][sel] = await page.locator(sel).count()
                    except Exception as e:  # noqa: BLE001
                        report[key][sel] = f"error: {e}"
            return report

        return self.run(_check, timeout=90)


class BrowserResponse:
    """httpx-like response for CanvasClient."""

    def __init__(self, status: int, headers: dict, text: str):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        # Cookie-authenticated Canvas API responses are prefixed with `while(1);`
        # (JSON-hijacking protection) and must be stripped before parsing.
        self.text = text[len("while(1);"):] if text.startswith("while(1);") else text

    def json(self) -> Any:
        return json.loads(self.text)


class BrowserTransport:
    """Canvas Transport that rides on the logged-in background profile."""

    def __init__(self, lane: BrowserLane):
        self.lane = lane

    def get(self, url: str, params=None) -> BrowserResponse:
        return self.lane.fetch(url, params)


_LANES: dict[int, BrowserLane] = {}


def get_lane(cfg) -> BrowserLane:
    key = id(cfg)
    if key not in _LANES:
        _LANES[key] = BrowserLane(cfg)
    return _LANES[key]


def browser_transport(cfg) -> BrowserTransport:
    return BrowserTransport(get_lane(cfg))


def interactive_login(cfg, site: str) -> int:
    """Open the background profile visibly (no debugging port) to sign in once."""
    urls = {"claude": "https://claude.ai/login", "canvas": cfg.canvas.base_url or None}
    url = urls.get(site, site if site.startswith("http") else None)
    if not url:
        print(f"Unknown site {site!r}. Use claude, canvas (needs [canvas].base_url), or a full URL.")
        return 2
    lane = BrowserLane(cfg)
    if lane._cdp_alive():
        print(f"The background browser is running on port {cfg.browser.cdp_port}. Stop the MCP server "
              "(or quit that Chrome) first, since a profile can only be open once.")
        return 1
    cfg.browser.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f"Opening {url} in the Instinct profile ({cfg.browser.profile_dir}).\n"
          "Sign in, then quit that Chrome window (⌘Q) to finish.")
    rc = subprocess.call([cfg.browser.chrome_path, f"--user-data-dir={cfg.browser.profile_dir}",
                          "--no-first-run", "--no-default-browser-check", url])
    print("Done. Run `uv run instinct doctor` to confirm the login was saved.")
    return rc
