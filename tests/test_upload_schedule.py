"""Tests for YouTube publish scheduling timezone parsing."""

from __future__ import annotations

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "upload"))

from youtube_schedule import AUTO_PUBLISH_TIMES, DEFAULT_PUBLISH_TZ, parse_publish_at, resolve_publish_schedule


def test_parse_publish_at_aest_winter() -> None:
    # June = AEST (UTC+10)
    assert parse_publish_at("2026-06-12 17:00", "Australia/Sydney") == "2026-06-12T07:00:00.000Z"


def test_parse_publish_at_aedt_summer() -> None:
    # January = AEDT (UTC+11)
    assert parse_publish_at("2026-01-15 17:00", "Australia/Sydney") == "2026-01-15T06:00:00.000Z"


def test_parse_publish_at_default_timezone_is_sydney() -> None:
    assert DEFAULT_PUBLISH_TZ == "Australia/Sydney"
    assert parse_publish_at("2026-06-12 17:00") == "2026-06-12T07:00:00.000Z"


def test_parse_publish_at_accepts_iso_t_separator() -> None:
    assert parse_publish_at("2026-06-12T17:00", "Australia/Sydney") == "2026-06-12T07:00:00.000Z"


def test_parse_publish_at_invalid_format_raises() -> None:
    with pytest.raises(ValueError, match="Unrecognized publish-at"):
        parse_publish_at("not-a-date", "Australia/Sydney")


def test_parse_publish_at_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        parse_publish_at("2026-06-12 17:00", "Not/A/Timezone")


def test_resolve_publish_schedule_auto_uses_next_future_sydney_morning() -> None:
    # 10:00, 16:30, and 21:00 are the daily slots
    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="auto",
        timezone=DEFAULT_PUBLISH_TZ,
        meta={},
        privacy="public",
        start_date=date(2026, 6, 19),
        occupied_dates={"2026-06-19"},
    )

    assert privacy == "private"
    assert utc == "2026-06-20T00:00:00.000Z"  # 10:00 AEST = 00:00 UTC
    assert tz == DEFAULT_PUBLISH_TZ
    assert local == "2026-06-20 10:00"


def test_resolve_publish_schedule_auto_skips_today_after_slot_time() -> None:
    # Before 10:00 today -> should get 10:00 today
    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="auto",
        timezone=DEFAULT_PUBLISH_TZ,
        meta={},
        privacy="public",
        start_date=date(2026, 6, 19),
        occupied_dates=set(),
        now=datetime(2026, 6, 19, 9, 0, tzinfo=ZoneInfo(DEFAULT_PUBLISH_TZ)),
    )

    assert privacy == "private"
    assert utc == "2026-06-19T00:00:00.000Z"  # 10:00 AEST = 00:00 UTC
    assert tz == DEFAULT_PUBLISH_TZ
    assert local == "2026-06-19 10:00"


def test_resolve_publish_schedule_auto_skips_10am_after_10am() -> None:
    # After 10:00 today but before 16:30 -> should get 16:30 today
    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="auto",
        timezone=DEFAULT_PUBLISH_TZ,
        meta={},
        privacy="public",
        start_date=date(2026, 6, 19),
        occupied_dates=set(),
        now=datetime(2026, 6, 19, 12, 0, tzinfo=ZoneInfo(DEFAULT_PUBLISH_TZ)),
    )

    assert privacy == "private"
    assert utc == "2026-06-19T06:30:00.000Z"  # 16:30 AEST = 06:30 UTC
    assert tz == DEFAULT_PUBLISH_TZ
    assert local == "2026-06-19 16:30"


def test_resolve_publish_schedule_auto_skips_today_after_1630() -> None:
    # After 16:30 today -> should get 21:00 today
    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="auto",
        timezone=DEFAULT_PUBLISH_TZ,
        meta={},
        privacy="public",
        start_date=date(2026, 6, 19),
        occupied_dates=set(),
        now=datetime(2026, 6, 19, 16, 31, tzinfo=ZoneInfo(DEFAULT_PUBLISH_TZ)),
    )

    assert privacy == "private"
    assert utc == "2026-06-19T11:00:00.000Z"  # 21:00 AEST = 11:00 UTC
    assert tz == DEFAULT_PUBLISH_TZ
    assert local == "2026-06-19 21:00"


def test_resolve_publish_schedule_auto_skips_consecutive_occupied_slots() -> None:
    # All three slots on 19th occupied -> should get 10:00 on 20th
    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="auto",
        timezone=DEFAULT_PUBLISH_TZ,
        meta={},
        privacy="public",
        start_date=date(2026, 6, 19),
        occupied_dates={"2026-06-19 10:00", "2026-06-19 16:30", "2026-06-19 21:00"},
    )

    assert privacy == "private"
    assert utc == "2026-06-20T00:00:00.000Z"
    assert tz == DEFAULT_PUBLISH_TZ
    assert local == "2026-06-20 10:00"


def test_resolve_publish_schedule_forces_private() -> None:
    from youtube_schedule import resolve_publish_schedule

    privacy, utc, tz, local = resolve_publish_schedule(
        publish_at="2026-06-12 17:00",
        timezone="Australia/Sydney",
        meta={},
        privacy="public",
    )
    assert privacy == "private"
    assert utc == "2026-06-12T07:00:00.000Z"
    assert tz == "Australia/Sydney"
    assert local == "2026-06-12 17:00"


def test_ensure_shorts_hashtag() -> None:
    from upload_youtube_shorts import ensure_shorts_hashtag

    title, desc = ensure_shorts_hashtag("ropz Nuke", "POV highlights")
    assert title == "ropz Nuke #Shorts"
    assert desc.endswith("#Shorts")
    title2, desc2 = ensure_shorts_hashtag("Already #Shorts", "Has #Shorts tag")
    assert title2 == "Already #Shorts"
    assert desc2 == "Has #Shorts tag"
