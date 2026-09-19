"""Time helpers shared by adapters."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
# Seconds-since-2001 values stay below ~1e10 for centuries; nanosecond values
# from any date after 2001-01-01 00:00:10 exceed 1e10. Anything bigger than
# this threshold is treated as nanoseconds.
_NS_THRESHOLD = 10_000_000_000


def apple_to_datetime(value: int | float | None) -> datetime | None:
    """Messages timestamps: ns since 2001-01-01 UTC (High Sierra+), seconds on older rows."""
    if value is None or value == 0:
        return None
    seconds = value / 1e9 if abs(value) > _NS_THRESHOLD else float(value)
    return APPLE_EPOCH + timedelta(seconds=seconds)


def datetime_to_apple_ns(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.astimezone()
    delta = dt - APPLE_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def datetime_to_apple_s(dt: datetime) -> int:
    return datetime_to_apple_ns(dt) // 1_000_000_000


_REL = re.compile(r"^\s*(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days|w|wk|wks|week|weeks)\s*(ago)?\s*$", re.I)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_since(value: str | None, now: datetime | None = None) -> datetime | None:
    """Accept ISO dates/datetimes, 'today', 'yesterday', or relative '30m', '6h', '2d', '1w'."""
    if value is None or not str(value).strip():
        return None
    now = now or datetime.now().astimezone()
    s = str(value).strip()
    low = s.lower()
    if low == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if low == "yesterday":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    if m := _REL.match(s):
        unit = _UNITS[m.group(2)[0].lower()]
        return now - timedelta(**{unit: int(m.group(1))})
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"can't parse time {value!r}; use ISO 8601, 'today', or e.g. '2d', '6h'") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)


def iso_local(dt: datetime | None) -> str | None:
    return dt.astimezone().isoformat(timespec="seconds") if dt else None
