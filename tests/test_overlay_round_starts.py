"""Regression: warmup round_start at tick 0 must not become overlay round 1."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

from overlay_pov import (  # noqa: E402
    TICKRATE,
    _load_pov_play_tick_ranges,
    _load_round_tick_ranges_events,
)

DEMO = Path(
    r"D:\Projects\CS2Archive\demos\hltv"
    r"\2395491-parivision-vs-faze-xse-pro-league-guangzhou"
    r"\parivision-vs-faze-m1-cache.dem"
)
STEAM_ID = "76561198016255205"
ANALYSIS = Path(
    r"D:\Projects\CS2Archive\renders"
    r"\pov-parivision-vs-faze-m1-cache_Twistzz\csdm_analysis.json"
)
VIDEO_DUR = 2202.456599


def test_warmup_round_start_filtered() -> None:
    if not DEMO.is_file():
        print("SKIP: demo not on disk")
        return
    ranges = _load_round_tick_ranges_events(DEMO)
    assert 0 not in {t for t, _ in ranges.values()}, "tick-0 start still present"
    assert len(ranges) == 22, f"expected 22 rounds, got {len(ranges)}"
    # Real pistol round starts near csdm startTick 1017, not 0.
    assert ranges[1][0] > 0
    assert abs(ranges[1][0] - 1016) <= 2


def test_play_ranges_align_with_video_and_csdm() -> None:
    if not DEMO.is_file() or not ANALYSIS.is_file():
        print("SKIP: demo/analysis not on disk")
        return
    play = _load_pov_play_tick_ranges(DEMO, STEAM_ID)
    analysis = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    assert len(play) == len(analysis["rounds"]) == 22

    total = sum((pe - ps) / TICKRATE for ps, pe in play.values())
    assert abs(total - VIDEO_DUR) < 1.0, (
        f"play-range total {total:.3f}s drifts from video {VIDEO_DUR:.3f}s"
    )
    # Round 1 must be real POV play (~60s), not the 2s phantom from tick-0.
    r1 = (play[1][1] - play[1][0]) / TICKRATE
    assert r1 > 30, f"round 1 only {r1:.1f}s — still looks like warmup phantom"


if __name__ == "__main__":
    test_warmup_round_start_filtered()
    test_play_ranges_align_with_video_and_csdm()
    print("PASS")
