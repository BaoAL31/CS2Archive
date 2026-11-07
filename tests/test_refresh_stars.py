from __future__ import annotations

import sys
import asyncio
from pathlib import Path


from cs2archive.misc.scrape_pov_channels import DEFAULT_CHANNELS
import cs2archive.hltv.refresh_stars as refresh_stars


def test_player_scrape_includes_own_and_competitors():
    assert "@cs2povarchive" in DEFAULT_CHANNELS
    assert len(DEFAULT_CHANNELS) >= 9
    assert refresh_stars.DEFAULT_CHANNELS is DEFAULT_CHANNELS


def test_task_command_points_at_launcher():
    cmd = refresh_stars._task_command()
    assert "run_refresh_stars.ps1" in cmd
    assert refresh_stars.TASK_NAME == "CS2ArchiveStarRefresh"
    assert refresh_stars.DEFAULT_AT == "12:00"


def test_player_refresh_keeps_own_outcome_counts_current_beyond_seven_days(monkeypatch):
    from types import SimpleNamespace
    from cs2archive.faceit import update_player_demand as demand

    calls = []
    old_own = {"video_id": "old-own", "published_at": "2026-09-01T00:00:00Z"}
    async def collect(channels, api_key, days, max_videos, skip_ids=None):
        calls.append((channels, days, skip_ids or set()))
        return [], [{"video_id": "own" if "@cs2povarchive" in channels else "competitor"}]

    monkeypatch.setattr(demand, "settings", SimpleNamespace(youtube_api_key="test"))
    monkeypatch.setattr(demand, "collect", collect)
    rows = asyncio.run(demand.scrape_new([old_own], 7))
    own_calls = [call for call in calls if "@cs2povarchive" in call[0]]
    assert len(own_calls) == 1
    assert own_calls[0][1:] == (180, set())
    assert any(days == 7 and "old-own" in skips for _, days, skips in calls)
    assert {row["video_id"] for row in rows} == {"own", "competitor"}
