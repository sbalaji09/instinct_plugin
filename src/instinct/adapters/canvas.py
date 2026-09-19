"""Canvas LMS adapter.

Primary path: REST API with a personal access token (Bearer). The HTTP layer is
a pluggable `Transport`, so the same client logic can run over the logged-in
background browser profile (cookie-authenticated same-origin requests, which is
what Canvas' own web UI does) when a school disables personal tokens. See
`browser_transport()` in adapters/browser.py.

Pagination follows the `Link: <...>; rel="next"` header. Throttling is `429`
(older deployments: `403` with "Rate Limit Exceeded"), retried with backoff;
we also slow down when `X-Rate-Limit-Remaining` gets low.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx

from instinct.timeutil import iso_local, parse_since

log = logging.getLogger("instinct.canvas")

MAX_PAGES = 50
PER_PAGE = 100
LOW_QUOTA = 50.0  # X-Rate-Limit-Remaining below this -> pause briefly between pages


class CanvasError(RuntimeError):
    pass


class CanvasAuthError(CanvasError):
    pass


class Response(Protocol):
    status_code: int
    headers: Any
    text: str

    def json(self) -> Any: ...


class Transport(Protocol):
    def get(self, url: str, params: list[tuple[str, str]] | None = None) -> Response: ...


class TokenTransport:
    """httpx client with a Bearer token."""

    def __init__(self, token: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, url: str, params=None) -> httpx.Response:
        return self._client.get(url, params=params, headers=self._headers)


_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="?([^";]+)"?')


def next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for url, rel in _LINK.findall(link_header):
        if "next" in rel.split():
            return url
    return None


def _is_rate_limited(r: Response) -> bool:
    return r.status_code == 429 or (r.status_code == 403 and "rate limit exceeded" in (r.text or "").lower())


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str | None, limit: int = 4000) -> str:
    if not s:
        return ""
    p = _TextExtractor()
    p.feed(s)
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit] + "…"


class CanvasClient:
    def __init__(self, base_url: str, transport: Transport, *, max_retries: int = 4,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] | None = None):
        if not base_url:
            raise CanvasError("Canvas base_url is not configured ([canvas].base_url)")
        self.base = base_url.rstrip("/")
        self.t = transport
        self.max_retries = max_retries
        self.sleep = sleep
        self.now = now or (lambda: datetime.now().astimezone())
        self._courses: dict[int, dict] | None = None

    # ------------------------------------------------------------------ http

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base}{path}"

    def _get(self, url: str, params=None) -> Response:
        for attempt in range(self.max_retries + 1):
            try:
                r = self.t.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise CanvasError(f"network error talking to Canvas: {type(e).__name__}") from e
                self.sleep(2 ** attempt)
                continue
            if _is_rate_limited(r) or r.status_code >= 500:
                if attempt == self.max_retries:
                    break
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                log.warning("canvas %s (attempt %d), retrying in %.1fs", r.status_code, attempt + 1, delay)
                self.sleep(delay)
                continue
            if r.status_code == 401:
                raise CanvasAuthError("Canvas rejected the credentials (401). Re-create the token and run "
                                      "`uv run instinct set-canvas-token`, or check `instinct doctor`.")
            if r.status_code >= 400:
                raise CanvasError(f"Canvas returned HTTP {r.status_code} for {url.split('?')[0]}")
            remaining = r.headers.get("X-Rate-Limit-Remaining")
            try:
                if remaining is not None and float(remaining) < LOW_QUOTA:
                    self.sleep(1.0)
            except ValueError:
                pass
            return r
        raise CanvasError(f"Canvas kept throttling/failing (HTTP {r.status_code}) after {self.max_retries} retries")

    def get_all(self, path: str, params: list[tuple[str, str]] | None = None) -> list[Any]:
        """GET every page of a list endpoint."""
        params = list(params or [])
        if not any(k == "per_page" for k, _ in params):
            params.append(("per_page", str(PER_PAGE)))
        url: str | None = self._url(path)
        out: list[Any] = []
        pages = 0
        while url:
            # The `next` link already carries the query string (and a page cursor).
            r = self._get(url, params if pages == 0 else None)
            data = r.json()
            if isinstance(data, dict) and "errors" in data:
                raise CanvasError(f"Canvas error: {data['errors']}")
            if not isinstance(data, list):
                raise CanvasError(f"expected a list from {path}, got {type(data).__name__}")
            out.extend(data)
            pages += 1
            url = next_link(r.headers.get("Link"))
            if pages >= MAX_PAGES:
                log.warning("stopping pagination of %s after %d pages", path, pages)
                break
        return out

    # ------------------------------------------------------------------ courses

    def courses(self) -> dict[int, dict]:
        if self._courses is None:
            data = self.get_all("/api/v1/courses", [("enrollment_state", "active")])
            self._courses = {c["id"]: c for c in data if "id" in c and not c.get("access_restricted_by_date")}
        return self._courses

    def _course_name(self, course_id) -> str | None:
        if course_id is None:
            return None
        c = self.courses().get(int(course_id))
        return (c.get("course_code") or c.get("name")) if c else None

    def resolve_course(self, course: str | int) -> dict:
        courses = self.courses()
        s = str(course).strip()
        if s.isdigit() and int(s) in courses:
            return courses[int(s)]
        low = s.lower()
        exact = [c for c in courses.values() if low in {(c.get("course_code") or "").lower(), (c.get("name") or "").lower()}]
        if exact:
            return exact[0]
        norm = re.sub(r"[\s\-_]", "", low)
        partial = [c for c in courses.values()
                   if norm in re.sub(r"[\s\-_]", "", f"{c.get('course_code', '')} {c.get('name', '')}".lower())]
        if len(partial) == 1:
            return partial[0]
        names = sorted(f"{c.get('course_code')} ({c['id']})" for c in (partial or courses.values()))
        if partial:
            raise CanvasError(f"{course!r} matches several courses: {', '.join(names)}")
        raise CanvasError(f"no active course matching {course!r}. Active courses: {', '.join(names)}")

    # ------------------------------------------------------------------ tools

    def upcoming(self, days: int = 7) -> list[dict]:
        start = self.now()
        end = start + timedelta(days=days)
        items = self.get_all("/api/v1/planner/items", [
            ("start_date", start.isoformat(timespec="seconds")),
            ("end_date", end.isoformat(timespec="seconds")),
        ])
        out = []
        for it in items:
            p = it.get("plannable") or {}
            subs = it.get("submissions") if isinstance(it.get("submissions"), dict) else {}
            out.append({
                "type": it.get("plannable_type"),
                "title": p.get("title") or p.get("name"),
                "course": it.get("context_name") or self._course_name(it.get("course_id")),
                "due_at": iso_local(_parse_dt(it.get("plannable_date") or p.get("due_at") or p.get("todo_date"))),
                "points_possible": p.get("points_possible"),
                "submitted": subs.get("submitted"),
                "missing": subs.get("missing"),
                "graded": subs.get("graded"),
                "completed": bool((it.get("planner_override") or {}).get("marked_complete")),
                "url": _abs(self.base, it.get("html_url")),
            })
        out.sort(key=lambda x: x["due_at"] or "")
        return out

    def todo(self) -> list[dict]:
        items = self.get_all("/api/v1/users/self/todo")
        out = []
        for it in items:
            a = it.get("assignment") or it.get("quiz") or {}
            out.append({
                "type": it.get("type"),  # "submitting" or "grading"
                "title": a.get("name") or a.get("title"),
                "course": self._course_name(it.get("course_id")) or it.get("context_name"),
                "due_at": iso_local(_parse_dt(a.get("due_at"))),
                "points_possible": a.get("points_possible"),
                "needs_grading_count": it.get("needs_grading_count"),
                "url": _abs(self.base, it.get("html_url") or a.get("html_url")),
            })
        out.sort(key=lambda x: x["due_at"] or "9999")
        return out

    def course_assignments(self, course: str | int, include_past: bool = False) -> dict:
        c = self.resolve_course(course)
        params = [("order_by", "due_at"), ("include[]", "submission")]
        if not include_past:
            params.append(("bucket", "future"))
        data = self.get_all(f"/api/v1/courses/{c['id']}/assignments", params)
        assignments = []
        for a in data:
            sub = a.get("submission") or {}
            assignments.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "due_at": iso_local(_parse_dt(a.get("due_at"))),
                "lock_at": iso_local(_parse_dt(a.get("lock_at"))),
                "points_possible": a.get("points_possible"),
                "submission_types": a.get("submission_types"),
                "submitted": sub.get("workflow_state") in {"submitted", "graded", "pending_review"} or None,
                "missing": sub.get("missing"),
                "score": sub.get("score"),
                "url": a.get("html_url"),
                "description": html_to_text(a.get("description"), limit=1500),
            })
        return {"course": {"id": c["id"], "code": c.get("course_code"), "name": c.get("name")},
                "assignments": assignments}

    def announcements(self, since: str | None = "14d") -> list[dict]:
        start = parse_since(since) or (self.now() - timedelta(days=14))
        courses = self.courses()
        if not courses:
            return []
        params = [("context_codes[]", f"course_{cid}") for cid in courses]
        params += [("start_date", start.date().isoformat()),
                   ("end_date", (self.now() + timedelta(days=1)).date().isoformat()),
                   ("active_only", "true")]
        data = self.get_all("/api/v1/announcements", params)
        out = []
        for a in data:
            posted = _parse_dt(a.get("posted_at") or a.get("delayed_post_at"))
            if posted and posted < start:
                continue
            code = a.get("context_code", "")
            cid = int(code.split("_", 1)[1]) if code.startswith("course_") else None
            out.append({
                "id": a.get("id"),
                "course": self._course_name(cid),
                "title": a.get("title"),
                "posted_at": iso_local(posted),
                "author": (a.get("author") or {}).get("display_name") or a.get("user_name"),
                "message": html_to_text(a.get("message")),
                "url": a.get("html_url"),
            })
        out.sort(key=lambda x: x["posted_at"] or "", reverse=True)
        return out


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _abs(base: str, url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else f"{base}{url}"


def default_client(cfg) -> CanvasClient:
    """Build the configured Canvas backend (API token by default, browser as fallback)."""
    from instinct.config import canvas_token

    if cfg.canvas.backend == "browser":
        from instinct.adapters.browser import browser_transport

        return CanvasClient(cfg.canvas.base_url, browser_transport(cfg), max_retries=cfg.canvas.max_retries)
    token = canvas_token()
    if not token:
        raise CanvasAuthError("No Canvas token. Run `uv run instinct set-canvas-token` or set "
                              "INSTINCT_CANVAS_TOKEN (or set [canvas].backend = \"browser\").")
    return CanvasClient(cfg.canvas.base_url, TokenTransport(token), max_retries=cfg.canvas.max_retries)
