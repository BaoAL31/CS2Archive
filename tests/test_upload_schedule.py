"""Tests for YouTube publish scheduling timezone parsing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from youtube_schedule import DEFAULT_PUBLISH_TZ, parse_publish_at


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
