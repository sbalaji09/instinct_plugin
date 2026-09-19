"""Logging to ~/.instinct/instinct.log (never stdout: stdout is the MCP channel).

Message bodies and other personal content are redacted unless
[safety].log_message_bodies is true.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_BODIES = False


def setup_logging(home: Path, log_bodies: bool = False, level: int = logging.INFO) -> None:
    global _LOG_BODIES
    _LOG_BODIES = log_bodies
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = logging.getLogger("instinct")
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(home / "instinct.log")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    eh = logging.StreamHandler(sys.stderr)
    eh.setFormatter(fmt)
    eh.setLevel(logging.WARNING)
    root.addHandler(eh)


def redact(text: str | None) -> str:
    """Body text as it may appear in logs."""
    if text is None:
        return "<none>"
    return text if _LOG_BODIES else f"<redacted {len(text)} chars>"
