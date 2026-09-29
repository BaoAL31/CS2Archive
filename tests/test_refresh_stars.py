from __future__ import annotations

import sys
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
