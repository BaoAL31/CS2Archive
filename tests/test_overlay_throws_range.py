"""Throw loading must not be amputated by mixed-tick-space bounds.

Split demos (p1/p2) restart tick numbering per part while
round_tick_ranges merges both parts: p1 spans 1148-107342 here while round
21 (a p2 round) ends at 70216. Bounding p1's throws by the last round's end
silently dropped 15 of NiKo's 34 p1 throws (renderer made 19 clips, the
validator expected 34). The render path loads unfiltered; per-round
windowing happens downstream in select_window_throws.
"""
from __future__ import annotations

from pathlib import Path


def test_render_path_loads_throws_unfiltered(monkeypatch, tmp_path):
    import cs2archive.overlay.overlay_utilcams as ou

    calls: dict = {}

    def fake_load(demo_path, steam_id, lo, hi):
        calls["lo"], calls["hi"] = lo, hi
        return []

    monkeypatch.setattr(ou, "_load_player_throws", fake_load)
    out = ou._render_throw_flight_clips(
        Path("x.dem"), "76561198041683378", 60.0, 100,
        tmp_path,
        round_offsets={1: 0.0, 21: 900.0},
        round_tick_ranges={1: (1148, 5518), 21: (1, 70216)},
    )
    assert out == []
    assert (calls["lo"], calls["hi"]) == (0, 0)


def test_mixed_space_late_throws_survive_windowing():
    """A p1 throw past a p2 round's end tick still maps into its p1 window."""
    from cs2archive.overlay.overlay_utilcams import _play_window_for_throw

    ranges = {1: (1148, 5518), 10: (75000, 82000), 21: (1, 70216)}
    assert _play_window_for_throw(80000, ranges) == 10
    assert _play_window_for_throw(2000, ranges) == 1
    assert _play_window_for_throw(200000, ranges) is None
