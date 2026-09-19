import pytest

from instinct.config import Config
from instinct.router import Router


class FakeProbe:
    def __init__(self, **flags):
        self.f = {"messages": True, "token": True, "canvas_web": False, "claude_web": False,
                  "key": True, "cli": True, "cua": False, **flags}

    def messages_db_readable(self): return self.f["messages"]
    def canvas_token(self): return self.f["token"]
    def browser_logged_in(self, site): return self.f["claude_web" if site == "claude" else "canvas_web"]
    def anthropic_key(self): return self.f["key"]
    def claude_cli(self): return self.f["cli"]
    def cua_driver(self): return self.f["cua"]


def route(intent, backend="auto", **flags):
    cfg = Config()
    cfg.canvas.backend = backend
    return Router(cfg, FakeProbe(**flags)).route(intent).to_dict()


@pytest.mark.parametrize("intent,tool,args", [
    ("what did Sam text me?", "messages_read_thread", {"chat": "Sam"}),
    ("did Mom message me today", "messages_read_thread", {"chat": "Mom"}),
    ("any new texts?", "messages_whats_new", {"mark_seen": False}),
    ("search my messages for dinner plans", "messages_search", {"text": "dinner plans"}),
])
def test_messages_intents_use_local_lane(intent, tool, args):
    r = route(intent)
    assert (r["capability"], r["lane"], r["tool"]) == ("messages", "local", tool)
    assert r["args"] == args


def test_messages_without_fda_has_no_gui_fallback():
    r = route("what did Sam text me", messages=False)
    assert r["lane"] is None and "Full Disk Access" in r["reason"]
    assert [o["lane"] for o in r["considered"]] == ["local"]


@pytest.mark.parametrize("intent,tool,args", [
    ("check canvas", "canvas_upcoming", {"days": 7}),
    ("what's due tomorrow", "canvas_upcoming", {"days": 2}),
    ("any new announcements", "canvas_announcements", {"since": "7d"}),
    ("show my canvas todo", "canvas_todo", {}),
    ("assignments for CSC 357", "canvas_course_assignments", {"course": "CSC 357"}),
])
def test_canvas_prefers_api(intent, tool, args):
    r = route(intent, canvas_web=True)
    assert (r["capability"], r["lane"], r["tool"], r["args"]) == ("canvas", "api", tool, args)


def test_canvas_falls_back_to_browser_without_token():
    r = route("check canvas", token=False, canvas_web=True)
    assert r["lane"] == "browser"
    assert "lower lanes unavailable" in r["reason"] and "no Canvas token" in r["reason"]


def test_canvas_backend_setting_is_respected():
    assert route("check canvas", backend="browser", canvas_web=True)["lane"] == "browser"
    assert route("check canvas", backend="api", token=False, canvas_web=True)["lane"] is None


def test_canvas_nothing_viable():
    r = route("check canvas", token=False)
    assert r["lane"] is None and r["reason"].startswith("no viable lane")


def test_claude_modes():
    r = route("ask claude to explain monads")
    assert (r["lane"], r["args"]) == ("api", {"prompt": "explain monads", "mode": "api"})
    assert route("ask claude hi", key=False)["args"]["mode"] == "cli"
    assert route("ask claude hi", key=False, cli=False, claude_web=True)["args"]["mode"] == "web"
    r = route("ask claude hi", key=False, cli=False, claude_web=False, cua=True)
    assert (r["lane"], r["args"]["mode"]) == ("gui", "desktop")


def test_claude_explicit_history_request_goes_to_web():
    r = route("ask claude about Rust lifetimes so it's in my claude.ai history", claude_web=True)
    assert (r["lane"], r["args"]["mode"]) == ("browser", "web")


def test_native_app_goes_to_gui_only_when_available():
    assert route("pause the song in Spotify", cua=True)["tool"] == "gui_get_app_state"
    assert route("pause the song in Spotify")["lane"] is None


def test_unknown():
    assert route("hmm")["capability"] == "unknown"
