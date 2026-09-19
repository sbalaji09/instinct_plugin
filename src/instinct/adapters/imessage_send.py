"""Send iMessages/SMS through Messages.app's AppleScript `send` command.

This is the only place Instinct writes to Messages. It never activates or
raises Messages: if the app isn't running it is launched hidden (`open -g -j`),
and `send` itself works without bringing a window forward.

Text and recipients are passed to osascript as argv, never interpolated into
the script, so message content can't inject AppleScript.

Needs the Automation permission (System Settings → Privacy & Security →
Automation → <your terminal / host app> → Messages). macOS asks the first time.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

from instinct.logs import redact

log = logging.getLogger("instinct.imessage_send")

MAX_LEN = 10_000

_SEND_TO_CHAT = """
on run argv
    tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)
end run
"""

_SEND_TO_HANDLE = """
on run argv
    tell application "Messages"
        if (item 3 of argv) is "SMS" then
            set svc to 1st account whose service type = SMS
        else
            set svc to 1st account whose service type = iMessage
        end if
        send (item 1 of argv) to participant (item 2 of argv) of svc
    end tell
end run
"""


class SendError(RuntimeError):
    pass


def looks_like_handle(s: str) -> bool:
    s = s.strip()
    return "@" in s or len(re.sub(r"\D", "", s)) >= 7 and not re.search(r"[A-Za-z]", s)


def _ensure_running() -> None:
    if subprocess.run(["pgrep", "-xq", "Messages"]).returncode == 0:
        return
    # -g: don't bring to foreground; -j: launch hidden.
    subprocess.run(["open", "-g", "-j", "-a", "Messages"], check=False)
    time.sleep(2.0)


def _osascript(script: str, *argv: str) -> None:
    try:
        out = subprocess.run(["osascript", "-", *argv], input=script, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise SendError("Messages didn't respond within 30s") from e
    if out.returncode != 0:
        err = out.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise SendError("Not allowed to control Messages. Enable System Settings → Privacy & Security → "
                            "Automation → (the app running Instinct) → Messages, then retry.")
        raise SendError(f"Messages refused the send: {err or 'unknown error'}")


def _check(text: str) -> None:
    if not text.strip():
        raise SendError("message text is empty")
    if len(text) > MAX_LEN:
        raise SendError(f"message is {len(text)} chars; limit is {MAX_LEN}")


def send_to_chat(chat_guid: str, text: str, fallback_handle: str | None = None, service: str = "iMessage") -> dict:
    """Send into an existing conversation by its chat.db guid.

    On macOS 26 some 1:1 chats have `any;-;<handle>` guids that AppleScript
    can't always address; for those we fall back to the participant handle.
    """
    _check(text)
    _ensure_running()
    try:
        _osascript(_SEND_TO_CHAT, text, chat_guid)
        via = "chat"
    except SendError as e:
        if not fallback_handle or "Automation" in str(e):
            raise
        log.info("send via chat id failed (%s); retrying via participant", e)
        _osascript(_SEND_TO_HANDLE, text, fallback_handle, service)
        via = "participant"
    log.info("sent %s via %s", redact(text), via)
    return {"sent": True, "via": via, "chars": len(text)}


def send_to_handle(handle: str, text: str, service: str = "iMessage") -> dict:
    """Send to a phone number or email that may not have a chat yet."""
    _check(text)
    _ensure_running()
    _osascript(_SEND_TO_HANDLE, text, handle.strip(), service)
    log.info("sent %s to a handle", redact(text))
    return {"sent": True, "via": "participant", "chars": len(text)}
