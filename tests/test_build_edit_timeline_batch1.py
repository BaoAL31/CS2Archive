"""Regression: _fix_edit_timeline produces golden batch-1 segments (Cache FACEIT match)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _extract_players_from_action_timeline,
    _fix_edit_timeline,
    _load_action_timeline,
)

COMPARE_KEYS = ("start_tick", "end_tick", "pov_steam_id", "segment_type", "kill_indices")


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fix_edit_timeline_batch1_matches_goal_lumped_llm():
    action_timeline = _load_action_timeline(FIXTURES / "cache_batch1_action_timeline.json")
    players = _extract_players_from_action_timeline(action_timeline)
    goal = _load_json("cache_batch1_goal_segments.json")["segments"]
    llm = {"segments": _load_json("cache_batch1_llm_segments.json")["segments"]}

    fixed = _fix_edit_timeline(llm, action_timeline, players)["segments"][: len(goal)]

    assert len(fixed) == len(goal)
    for i, (got, want) in enumerate(zip(fixed, goal)):
        for key in COMPARE_KEYS:
            assert got[key] == want[key], f"seg {i + 1} {key}: got {got[key]!r}, want {want[key]!r}"


def test_fix_edit_timeline_batch1_matches_goal_fragmented_llm():
    action_timeline = _load_action_timeline(FIXTURES / "cache_batch1_action_timeline.json")
    players = _extract_players_from_action_timeline(action_timeline)
    goal = _load_json("cache_batch1_goal_segments.json")["segments"]
    llm = {"segments": _load_json("cache_batch1_llm_fragmented.json")["segments"]}

    fixed = _fix_edit_timeline(llm, action_timeline, players)["segments"][: len(goal)]

    assert len(fixed) == len(goal)
    for i, (got, want) in enumerate(zip(fixed, goal)):
        for key in COMPARE_KEYS:
            assert got[key] == want[key], f"seg {i + 1} {key}: got {got[key]!r}, want {want[key]!r}"
