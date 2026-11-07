"""Shorts scheduling never yields days to long-form uploads."""
from __future__ import annotations

from cs2archive.upload.upload_youtube_shorts import shorts_occupied_dates


def test_long_form_days_stay_open_for_shorts():
    all_dates = {
        "2026-10-09T10:00",  # long-form premiere
        "2026-10-09T16:30",  # long-form premiere
        "2026-10-09T18:00",  # another Short
    }
    long_form = {"2026-10-09T10:00", "2026-10-09T16:30"}
    assert shorts_occupied_dates(all_dates, long_form) == {"2026-10-09T18:00"}


def test_empty_pool_means_nothing_blocked():
    assert shorts_occupied_dates(set(), set()) == set()
    assert shorts_occupied_dates(None, None) == set()


def test_all_shorts_pool_blocks_their_days():
    all_dates = {"2026-10-09T18:00"}
    assert shorts_occupied_dates(all_dates, set()) == {"2026-10-09T18:00"}
