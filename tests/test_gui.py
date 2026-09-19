import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import Client

from instinct.adapters.gui import CuaDriver, ForegroundRequired, GuiError, ask_claude_desktop
from instinct.config import Config
from instinct.server import create_server

FAKE = str(Path(__file__).parent / "fake_cua_driver.py")


@pytest.fixture
def driver_factory(tmp_path, monkeypatch):
    made = []

    def make(behavior=None):
        log = tmp_path / f"calls{len(made)}.jsonl"
        env = {"FAKE_CUA_LOG": str(log), "FAKE_CUA_BEHAVIOR": json.dumps(behavior or {}),
               "PATH": os.environ["PATH"]}
        d = CuaDriver(sys.executable, args=[FAKE], timeout=30, env=env)
        d.log = log
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def calls(driver):
    return [json.loads(line) for line in driver.log.read_text().splitlines()] if driver.log.exists() else []


def actions(driver):
    return [c for c in calls(driver) if c["tool"] not in ("list_windows", "get_window_state", "check_permissions")]


def test_state_and_background_click(driver_factory):
    d = driver_factory()
    st = d.get_app_state("mail")
    assert st["window_title"] == "t" and st["window_id"] == 11  # front-most Mail window
    tok = st["elements"][0]["element_token"]
    assert d.describe("Mail", tok).startswith("'AXButton Send'")
    out = d.click("Mail", tok)
    assert out["effect"] == "confirmed"
    (c,) = actions(d)
    assert c == {"tool": "click", "args": {"pid": 100, "window_id": 11, "element_token": tok, "action": "press",
                                           "delivery_mode": "background"}}


def test_numeric_ids_need_snapshot(driver_factory):
    d = driver_factory()
    with pytest.raises(GuiError, match="gui_get_app_state first"):
        d.click("Mail", "1")
    snap = d.get_app_state("Mail")["snapshot_id"]
    d.click("Mail", "1")
    assert actions(d)[0]["args"]["snapshot_id"] == snap and actions(d)[0]["args"]["element_index"] == 1


def test_keys(driver_factory):
    d = driver_factory()
    d.press_keys("Mail", "return")
    d.press_keys("Mail", "cmd + shift + T")
    assert [(a["tool"], a["args"].get("key") or a["args"].get("keys")) for a in actions(d)] == [
        ("press_key", "return"), ("hotkey", ["cmd", "shift", "t"])]
    with pytest.raises(GuiError):
        d.press_keys("Mail", "cmd+shift")


@pytest.mark.parametrize("behavior", [
    {"click": {"effect": "partial", "escalation": {"target": "foreground", "reason": "needs key window"}}},
    {"click": {"code": "background_unavailable", "effect": "refused"}},
])
def test_foreground_escalation_is_an_error_not_a_fallback(driver_factory, behavior):
    d = driver_factory(behavior)
    with pytest.raises(ForegroundRequired, match="never falls back"):
        d.click("Mail", "s0001:0")
    assert [a["tool"] for a in actions(d)] == ["click"]  # exactly one attempt, no retry
    assert all(a["args"]["delivery_mode"] == "background" for a in actions(d))


def test_forbidden_paths(driver_factory):
    d = driver_factory()
    with pytest.raises(GuiError, match="not allowed"):
        d.call("bring_to_front", {"pid": 100})
    with pytest.raises(ForegroundRequired):
        d.act("click", {"pid": 100, "target": {"kind": "desktop", "display_id": "primary"}})
    with pytest.raises(ForegroundRequired):
        d.act("click", {"pid": 100, "modifier": ["cmd"]})
    assert d.act("click", {"pid": 100, "delivery_mode": "foreground"})  # caller can't override...
    assert actions(d)[-1]["args"]["delivery_mode"] == "background"     # ...it is forced to background
    assert "bring_to_front" not in [c["tool"] for c in calls(d)]


def test_app_must_be_running(driver_factory):
    d = driver_factory()
    with pytest.raises(GuiError, match="never launches"):
        d.get_app_state("Spotify")


def test_screenshot(driver_factory):
    meta, png = driver_factory().screenshot_window("Mail")
    assert png.startswith(b"\x89PNG") and meta["window_id"] == 11


def test_ask_claude_desktop(driver_factory):
    d = driver_factory()
    out = ask_claude_desktop(d, "what is 2+2", timeout=20, poll_s=0.1)
    assert "The answer is 4." in out["window_text_after_send"] and out["experimental"]
    assert [a["tool"] for a in actions(d)] == ["type_text", "press_key"]


# --------------------------------------------------------------------------- server: nothing runs unconfirmed

SIDE_EFFECT_ARGS = {
    "gui_click": {"app": "Mail", "element_id": "s0001:0"},
    "gui_type_text": {"app": "Mail", "text": "hello"},
    "gui_press_keys": {"app": "Mail", "keys": "cmd+n"},
    "gui_scroll": {"app": "Mail", "direction": "down"},
    "claude_web_send": {"prompt": "hi"},
    "ask_claude": {"prompt": "hi", "mode": "desktop"},
}
NOT_GATED = {"confirm_action", "cancel_action", "messages_whats_new"}  # whats_new only moves a local cursor


def test_every_side_effect_tool_requires_confirmation(driver_factory, tmp_path):
    d = driver_factory()

    class NoBrowser:
        def claude_send(self, *a):
            raise AssertionError("browser used before confirmation")

    server = create_server(Config(home=tmp_path), gui=d, browser=NoBrowser())

    async def go():
        async with Client(server) as c:
            tools = (await c.list_tools()).tools
            side = {t.name for t in tools if not (t.annotations and t.annotations.read_only_hint)} - NOT_GATED
            # ask_claude is only gated for web/desktop modes; api/cli are plain queries
            assert side == set(SIDE_EFFECT_ARGS), side
            results = {}
            for name, args in SIDE_EFFECT_ARGS.items():
                r = await c.call_tool(name, args)
                results[name] = json.loads(r.content[0].text)
            pending = json.loads((await c.call_tool("list_pending_actions", {})).content[0].text)
            return results, pending

    results, pending = anyio.run(go)
    assert all(r["status"] == "pending_confirmation" for r in results.values())
    assert len(pending) == len(SIDE_EFFECT_ARGS)
    assert actions(d) == []  # nothing reached the driver
    assert "Mail window 'Inbox'" in results["gui_click"]["summary"]


def test_confirm_runs_gui_action_once(driver_factory, tmp_path):
    d = driver_factory()
    server = create_server(Config(home=tmp_path), gui=d)

    async def go():
        async with Client(server) as c:
            p = json.loads((await c.call_tool("gui_press_keys", {"app": "Mail", "keys": "cmd+n"})).content[0].text)
            assert actions(d) == []
            ok = await c.call_tool("confirm_action", {"action_id": p["action_id"]})
            again = await c.call_tool("confirm_action", {"action_id": p["action_id"]})
            return ok, again

    ok, again = anyio.run(go)
    assert not ok.is_error and again.is_error
    assert [a["tool"] for a in actions(d)] == ["hotkey"]


def test_foreground_error_surfaces_through_confirm(driver_factory, tmp_path):
    d = driver_factory({"scroll": {"code": "background_unavailable", "effect": "refused"}})
    server = create_server(Config(home=tmp_path), gui=d)

    async def go():
        async with Client(server) as c:
            p = json.loads((await c.call_tool("gui_scroll", {"app": "Mail", "direction": "down"})).content[0].text)
            return await c.call_tool("confirm_action", {"action_id": p["action_id"]})

    r = anyio.run(go)
    assert r.is_error and "never falls back to foreground" in r.content[0].text
