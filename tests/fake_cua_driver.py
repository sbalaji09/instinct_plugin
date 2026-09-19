"""Stand-in for `cua-driver mcp`: same tool names/shapes, records every call.

FAKE_CUA_LOG: JSONL file of calls. FAKE_CUA_BEHAVIOR: JSON {tool: response-override}.
"""

import base64
import json
import os
import sys

from mcp.server.mcpserver import MCPServer

LOG = os.environ["FAKE_CUA_LOG"]
BEHAVIOR = json.loads(os.environ.get("FAKE_CUA_BEHAVIOR", "{}"))
STATE = {"sent": False, "snap": 0}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

mcp = MCPServer("fake-cua-driver")


def record(tool, args):
    with open(LOG, "a") as f:
        f.write(json.dumps({"tool": tool, "args": args}) + "\n")


def respond(tool, default):
    return BEHAVIOR.get(tool, default)


@mcp.tool()
def check_permissions() -> dict:
    record("check_permissions", {})
    return {"accessibility": True, "screen_recording": True}


@mcp.tool()
def list_apps() -> dict:
    return {"apps": []}


@mcp.tool()
def list_windows(pid: int | None = None, on_screen_only: bool | None = None) -> dict:
    record("list_windows", {})
    return {"windows": [
        {"window_id": 11, "pid": 100, "app_name": "Mail", "title": "Inbox", "z_index": 5, "on_current_space": True},
        {"window_id": 12, "pid": 100, "app_name": "Mail", "title": "Drafts", "z_index": 2, "on_current_space": True},
        {"window_id": 21, "pid": 200, "app_name": "Claude", "title": "Claude", "z_index": 1, "on_current_space": True},
    ]}


@mcp.tool()
def get_window_state(pid: int, window_id: int, include_accessibility_tree: bool = True,
                     include_screenshot: bool = True, max_elements: int | None = None,
                     query: str | None = None) -> list:
    record("get_window_state", {"pid": pid, "window_id": window_id})
    STATE["snap"] += 1
    sid = f"s{STATE['snap']:04d}"
    if pid == 200:
        els = [{"element_index": 0, "element_token": f"{sid}:0", "role": "AXTextArea", "label": "Message Claude"}]
        tree = "- AXTextArea Message Claude"
        if STATE["sent"]:
            tree += "\n- AXStaticText The answer is 4."
    else:
        els = [{"element_index": 0, "element_token": f"{sid}:0", "role": "AXButton", "label": "Send",
                "actions": ["AXPress"]},
               {"element_index": 1, "element_token": f"{sid}:1", "role": "AXTextField", "label": "Subject"}]
        tree = "- AXButton Send\n- AXTextField Subject"
    out = [json.dumps({"snapshot_id": sid, "elements": els, "tree_markdown": tree, "app_name": "x",
                       "window_title": "t"})]
    if include_screenshot:
        from mcp.server.mcpserver import Image
        out.append(Image(data=PNG, format="png"))
    return out


def _action(tool, args):
    record(tool, args)
    if tool in ("type_text", "press_key") and args.get("pid") == 200:
        STATE["sent"] = tool == "press_key" or STATE["sent"]
    return respond(tool, {"effect": "confirmed", "route": "ax", "delivery": {"mode": args.get("delivery_mode")}})


@mcp.tool()
def click(pid: int, window_id: int | None = None, element_token: str | None = None, element_index: int | None = None,
          snapshot_id: str | None = None, action: str = "press", delivery_mode: str = "background",
          modifier: list[str] | None = None) -> dict:
    return _action("click", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def type_text(text: str, pid: int, window_id: int | None = None, element_token: str | None = None,
              element_index: int | None = None, snapshot_id: str | None = None,
              delivery_mode: str = "background") -> dict:
    return _action("type_text", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def press_key(key: str, pid: int, window_id: int | None = None, element_token: str | None = None,
              delivery_mode: str = "background") -> dict:
    return _action("press_key", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def hotkey(keys: list[str], pid: int, window_id: int | None = None, element_token: str | None = None,
           delivery_mode: str = "background") -> dict:
    return _action("hotkey", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def scroll(direction: str, pid: int, window_id: int | None = None, amount: int = 3, by: str = "line",
           element_token: str | None = None, delivery_mode: str = "background") -> dict:
    return _action("scroll", {k: v for k, v in locals().items() if v is not None})


@mcp.tool()
def set_value(pid: int, value: str, element_token: str | None = None) -> dict:
    return _action("set_value", {"pid": pid, "value": value})


@mcp.tool()
def bring_to_front(pid: int) -> dict:  # exists on the real driver; the wrapper must never call it
    record("bring_to_front", {"pid": pid})
    return {"ok": True}


if __name__ == "__main__":
    mcp.run("stdio")
