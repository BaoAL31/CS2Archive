"""Dead-gap cut plan (no ffmpeg)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.dead_gap_trim import GAP_MIN, KEEP_AFTER_KILL, RESUME_BEFORE_KILL, plan_cuts


def test_no_cut_when_kills_are_tight():
    assert plan_cuts([1.0, 3.0, 8.0], 20.0) == []


def test_cuts_gap_at_threshold():
    kills = [2.0, 2.0 + GAP_MIN]
    cuts = plan_cuts(kills, 80.0)
    assert len(cuts) == 1
    cs, ce = cuts[0]
    assert cs == 2.0 + KEEP_AFTER_KILL
    assert ce == kills[1] - RESUME_BEFORE_KILL


def test_cmtry_style_long_middle_gap():
    kills = [5.0, 8.2, 8.6, 125.7]
    cuts = plan_cuts(kills, 127.2)
    assert len(cuts) == 1
    cs, ce = cuts[0]
    assert cs == 8.6 + KEEP_AFTER_KILL
    assert ce == 125.7 - RESUME_BEFORE_KILL
    assert (ce - cs) > 90
