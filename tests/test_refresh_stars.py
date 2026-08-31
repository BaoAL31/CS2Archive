from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "misc"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "hltv"))

from scrape_pov_channels import DEFAULT_CHANNELS
import refresh_stars


def test_player_scrape_includes_own_and_competitors():
    assert "@cs2povarchive" in DEFAULT_CHANNELS
    assert len(DEFAULT_CHANNELS) >= 9
    assert refresh_stars.DEFAULT_CHANNELS is DEFAULT_CHANNELS


def test_task_command_points_at_launcher():
    cmd = refresh_stars._task_command()
    assert "run_refresh_stars.ps1" in cmd
    assert refresh_stars.TASK_NAME == "CS2ArchiveStarRefresh"
    assert refresh_stars.DEFAULT_AT == "12:00"
