"""Regression: fix-pass on rendered Cache timeline matches full golden kill sets + ticks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _extract_players_from_action_timeline,
    _fix_edit_timeline,
)

COMPARE_KEYS = ("start_tick", "end_tick", "pov_steam_id", "segment_type", "kill_indices")


@pytest.mark.skipif(
    not (RUN / "edit_timeline.rendered_73.json").is_file(),
    reason="rendered_73 baseline missing",
)
@pytest.mark.skipif(
    not (FIXTURES / "cache_full_goal_segments.json").is_file(),
    reason="full golden fixture missing",
)
@pytest.mark.skipif(
    not (RUN / "action_timeline.json").is_file(),
    reason="action_timeline missing",
)
def test_fix_edit_timeline_cache_full_matches_golden():
    at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
    players = _extract_players_from_action_timeline(at)
    rendered = json.loads((RUN / "edit_timeline.rendered_73.json").read_text(encoding="utf-8"))
    goal = json.loads((FIXTURES / "cache_full_goal_segments.json").read_text(encoding="utf-8"))["segments"]

    fixed = _fix_edit_timeline({"segments": rendered["segments"]}, at, players)["segments"]

    assert len(fixed) == len(goal)
    for i, (got, want) in enumerate(zip(fixed, goal)):
        for key in COMPARE_KEYS:
            assert got[key] == want[key], f"seg {i + 1} {key}: got {got[key]!r}, want {want[key]!r}"
