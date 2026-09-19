"""Router: map a high-level intent to the lowest viable lane.

Design rule (lower is better):
  1 api        official API or CLI
  2 local      local data store on disk
  3 browser    separate background Chrome profile over CDP
  4 gui        background native-app control via Cua Driver

The router does not execute anything. It classifies the intent, probes which
lanes are usable right now (cheap local checks only, no network), and returns
the tool to call plus the reason, logging the decision.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

log = logging.getLogger("instinct.router")

LANE_ORDER = {"api": 1, "local": 2, "browser": 3, "gui": 4}


class Probe:
    """Which lanes are usable. Override in tests."""

    def __init__(self, cfg):
        self.cfg = cfg

    def messages_db_readable(self) -> bool:
        try:
            with open(self.cfg.messages.db_path, "rb") as f:
                f.read(1)
            return True
        except OSError:
            return False

    def canvas_token(self) -> bool:
        from instinct.config import canvas_token

        return bool(self.cfg.canvas.base_url and canvas_token())

    def browser_logged_in(self, site: str) -> bool:
        from instinct.doctor import _profile_has_cookie

        host = "claude.ai" if site == "claude" else urlparse(self.cfg.canvas.base_url).hostname
        if not host or not self.cfg.browser.profile_dir.exists():
            return False
        try:
            return _profile_has_cookie(self.cfg.browser.profile_dir, host)
        except Exception:
            return False

    def anthropic_key(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def claude_cli(self) -> bool:
        return shutil.which(self.cfg.claude.claude_cli) is not None

    def cua_driver(self) -> bool:
        from instinct.adapters.gui import find_driver

        return find_driver(self.cfg) is not None


@dataclass
class Option:
    lane: str
    tool: str
    args: dict
    viable: bool
    why: str

    @property
    def rank(self) -> int:
        return LANE_ORDER[self.lane]


@dataclass
class Route:
    intent: str
    capability: str
    chosen: Option | None
    reason: str
    options: list[Option] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "capability": self.capability,
            "lane": self.chosen.lane if self.chosen else None,
            "tool": self.chosen.tool if self.chosen else None,
            "args": self.chosen.args if self.chosen else None,
            "reason": self.reason,
            "considered": [{**asdict(o), "rank": o.rank} for o in self.options],
        }


# --------------------------------------------------------------------------- classification

_MESSAGES = re.compile(r"\b(text(ed|s)?|imessages?|messages?|messaged|sms|dm(ed)?|chat|group ?chat)\b", re.I)
_CANVAS = re.compile(r"\b(canvas|assignments?|homework|hw|due|deadlines?|class(es)?|courses?|quiz(zes)?|"
                     r"announcements?|syllabus|grades?|lab|midterm|exam|to-?do)\b", re.I)
_CLAUDE = re.compile(r"\b(ask|tell|prompt|send( it)? to)\s+claude\b|\bclaude\b", re.I)
_WHO = re.compile(r"\b(?:did|has|have)\s+(.+?)\s+(?:text|message|say|send|write|reply)", re.I)
_FROM = re.compile(r"\b(?:from|with|to)\s+([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)?)")
_NEW = re.compile(r"\b(new|unread|missed|latest|recent|what'?s new|catch me up)\b", re.I)
_SEARCH = re.compile(r"\b(?:search|find)\b.*?\bfor\s+[\"“']?(.+?)[\"”']?$"
                     r"|\b(?:about|mention(?:ed)?|said)\s+[\"“']?(.+?)[\"”']?$", re.I)
_APP = re.compile(r"\b(?:in|on|using|open)\s+(?:the\s+)?([A-Z][\w.]*(?:\s+[A-Z][\w.]*)?)(?:\s+app)?\b")


def _messages_args(intent: str) -> tuple[str, dict]:
    who = _WHO.search(intent) or _FROM.search(intent)
    if who:
        name = re.sub(r"\b(me|my)\b", "", who.group(1), flags=re.I).strip(" ?.")
        if name:
            return "messages_read_thread", {"chat": name}
    if (s := _SEARCH.search(intent)) and not _NEW.search(intent):
        return "messages_search", {"text": (s.group(1) or s.group(2)).strip(" ?.")}
    return "messages_whats_new", {"mark_seen": False}


def _canvas_tool(intent: str) -> tuple[str, dict]:
    low = intent.lower()
    if "announcement" in low:
        return "canvas_announcements", {"since": "7d"}
    if re.search(r"\bto-?do\b", low):
        return "canvas_todo", {}
    m = re.search(r"\b([A-Z]{2,5})\s?-?(\d{3,4}[A-Z]?)\b", intent)  # course code like CSC 357
    if m:
        return "canvas_course_assignments", {"course": f"{m.group(1)} {m.group(2)}"}
    days = 1 if re.search(r"\b(today|tonight)\b", low) else 2 if "tomorrow" in low else 7
    return "canvas_upcoming", {"days": days}


class Router:
    def __init__(self, cfg, probe: Probe | None = None):
        self.cfg = cfg
        self.probe = probe or Probe(cfg)

    def classify(self, intent: str) -> str:
        if _CLAUDE.search(intent):
            return "claude"
        if _MESSAGES.search(intent) or _WHO.search(intent):
            return "messages"
        if _CANVAS.search(intent):
            return "canvas"
        if _APP.search(intent):
            return "app"
        return "unknown"

    def options(self, capability: str, intent: str) -> list[Option]:
        p = self.probe
        if capability == "messages":
            tool, args = _messages_args(intent)
            ok = p.messages_db_readable()
            return [
                Option("local", tool, args, ok,
                       "chat.db is readable" if ok else "chat.db not readable: grant Full Disk Access "
                                                        "(run `instinct doctor`)"),
                # Deliberately not offered: scrolling Messages.app through the GUI lane.
            ]
        if capability == "canvas":
            tool, args = _canvas_tool(intent)
            backend = self.cfg.canvas.backend
            token = p.canvas_token()
            logged_in = p.browser_logged_in("canvas")
            return [
                Option("api", tool, args, token and backend != "browser",
                       "backend=browser" if backend == "browser" else
                       ("Canvas token configured" if token else "no Canvas token / base_url")),
                Option("browser", tool, args, logged_in and backend != "api",
                       "backend=api" if backend == "api" else
                       ("background profile has a Canvas session" if logged_in
                        else "background profile not logged in to Canvas (`instinct login canvas`)")),
            ]
        if capability == "claude":
            low = intent.lower()
            prompt = re.sub(r"^.*?\b(ask|tell|prompt)\s+claude\b[\s:,]*(to\s+)?", "", intent, flags=re.I).strip()
            wants_web = bool(re.search(r"claude\.ai|history|web|browser", low))
            wants_desktop = bool(re.search(r"desktop app|claude app", low))
            api, cli = p.anthropic_key(), p.claude_cli()
            web, desk = p.browser_logged_in("claude"), p.cua_driver()
            opts = [
                Option("api", "ask_claude", {"prompt": prompt, "mode": "api"}, api and not (wants_web or wants_desktop),
                       "ANTHROPIC_API_KEY set" if api else "no ANTHROPIC_API_KEY"),
                Option("api", "ask_claude", {"prompt": prompt, "mode": "cli"}, cli and not (wants_web or wants_desktop),
                       "`claude` CLI on PATH" if cli else "`claude` CLI not found"),
                Option("browser", "ask_claude", {"prompt": prompt, "mode": "web"}, web and not wants_desktop,
                       "claude.ai session in background profile" if web else "background profile not logged in"),
                Option("gui", "ask_claude", {"prompt": prompt, "mode": "desktop"}, desk,
                       "Cua Driver available (experimental)" if desk else "Cua Driver not installed"),
            ]
            if wants_web or wants_desktop:
                for o in opts:
                    if o.lane in ("api",):
                        o.why += "; skipped: user asked for " + ("claude.ai history" if wants_web else "desktop app")
            return opts
        if capability == "app":
            app = _APP.search(intent).group(1)
            ok = p.cua_driver()
            return [Option("gui", "gui_get_app_state", {"app": app}, ok,
                           "Cua Driver available" if ok else "Cua Driver not installed (see `instinct doctor`)")]
        return []

    def route(self, intent: str) -> Route:
        cap = self.classify(intent)
        opts = sorted(self.options(cap, intent), key=lambda o: o.rank)  # stable: keeps api before cli
        chosen = next((o for o in opts if o.viable), None)
        if cap == "unknown":
            reason = ("Couldn't map this to Messages, Canvas, Claude, or a named app. Call a specific "
                      "tool directly.")
        elif chosen:
            skipped = [f"{o.lane}/{o.args.get('mode', o.tool)} ({o.why})" for o in opts
                       if o.rank < chosen.rank and not o.viable]
            reason = f"lane {chosen.rank} ({chosen.lane}): {chosen.why}"
            if skipped:
                reason += "; lower lanes unavailable: " + "; ".join(skipped)
        else:
            reason = "no viable lane: " + "; ".join(f"{o.lane}: {o.why}" for o in opts)
        r = Route(intent=intent, capability=cap, chosen=chosen, reason=reason, options=opts)
        log.info("route capability=%s lane=%s tool=%s reason=%s", cap, chosen and chosen.lane,
                 chosen and chosen.tool, reason)
        return r
