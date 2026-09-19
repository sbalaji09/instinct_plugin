from datetime import UTC, datetime, timedelta, timezone

import pytest

from instinct.timeutil import (apple_to_datetime, datetime_to_apple_ns, datetime_to_apple_s, parse_since)


def test_nanoseconds_since_2001():
    # 2024-01-01T00:00:00Z is 725760000 s after 2001-01-01
    assert apple_to_datetime(725_760_000_000_000_000) == datetime(2024, 1, 1, tzinfo=UTC)


def test_seconds_rows_detected():
    assert apple_to_datetime(725_760_000) == datetime(2024, 1, 1, tzinfo=UTC)
    # pre-High-Sierra era row in seconds
    assert apple_to_datetime(400_000_000) == datetime(2001, 1, 1, tzinfo=UTC) + timedelta(seconds=400_000_000)


def test_fractional_nanoseconds_and_nulls():
    dt = apple_to_datetime(725_760_000_123_456_789)
    assert dt.microsecond == 123_457 or dt.microsecond == 123_456
    assert apple_to_datetime(0) is None
    assert apple_to_datetime(None) is None


def test_round_trip_with_timezone():
    dt = datetime(2025, 3, 9, 1, 30, tzinfo=timezone(timedelta(hours=-8)))
    assert apple_to_datetime(datetime_to_apple_ns(dt)) == dt
    assert apple_to_datetime(datetime_to_apple_s(dt)) == dt


def test_parse_since():
    now = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)
    assert parse_since("2d", now) == now - timedelta(days=2)
    assert parse_since("6h", now) == now - timedelta(hours=6)
    assert parse_since("1 week ago", now) == now - timedelta(weeks=1)
    assert parse_since("today", now) == datetime(2026, 9, 18, tzinfo=UTC)
    assert parse_since("2026-09-01", now) == datetime(2026, 9, 1, tzinfo=UTC)
    assert parse_since("2026-09-01T10:00:00Z", now) == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert parse_since(None) is None
    with pytest.raises(ValueError):
        parse_since("last tuesday-ish", now)
