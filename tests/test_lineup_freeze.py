"""Once-per-lineup PiP dedupe, freeze sidecar expansion, keyboard branding.

Pure-python coverage (no CS2/demo/CS2UtilArchive needed — the CS2Util
bridge is monkeypatched out so the stdlib fallbacks are exercised).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from overlay.lineup_freeze import (  # noqa: E402
    dedupe_throws_by_lineup,
    expand_offsets_for_freezes,
    classify_throws_straightforward,
    collapse_frame,
    expand_frame,
    freeze_frame_plan,
    is_intuitive_lob,
    reconstruct_pristine_timeline,
    select_window_throws,
    split_straightforward,
)


def _without_cs2util(monkeypatch: pytest.MonkeyPatch):
    import overlay.lineup_freeze as lf
    monkeypatch.setattr(lf, "_cs2util", lambda name: None)


def _throw(tid, tick, util="smoke", x=0.0, y=0.0, z=0.0, **kw):
    # Default landing is spread by tick so fixtures only collapse when a
    # test sets land_* explicitly (landing match == same lineup).
    row = {
        "throw_id": tid,
        "throw_tick": tick,
        "util_type": util,
        "release_x": x,
        "release_y": y,
        "release_z": z,
        "land_x": 5000.0 + tick,
        "land_y": 5000.0,
        "land_z": 0.0,
        "is_renderable": True,
        "flight_ticks": 100,
    }
    row.update(kw)
    return row


def test_dedupe_keeps_earliest_per_release_cell():
    throws = [
        _throw("a", 1000, x=10, y=20, z=30),
        _throw("b", 2000, x=15, y=25, z=32),  # same cell as a
        _throw("c", 3000, x=5000, y=20, z=30),  # far cell, same util -> still dropped
        _throw("d", 4000, util="flash", x=10, y=20, z=30),  # other type: separate
    ]
    kept = dedupe_throws_by_lineup(throws)
    assert [t["throw_id"] for t in kept] == ["a", "d"]


def test_dedupe_idempotent():
    throws = [
        _throw("a", 1000, x=10, y=20, z=30),
        _throw("b", 2000, x=15, y=25, z=32),
        _throw("c", 3000, x=5000, y=20, z=30),
    ]
    once = dedupe_throws_by_lineup(throws)
    twice = dedupe_throws_by_lineup(once)
    assert [t["throw_id"] for t in once] == [t["throw_id"] for t in twice]


def test_dedupe_keeps_throws_without_release_coords():
    throws = [
        _throw("a", 1000, x=10, y=20, z=30),
        {"throw_id": "mystery", "throw_tick": 1500, "util_type": "flash",
         "is_renderable": True, "flight_ticks": 50},
    ]
    kept = dedupe_throws_by_lineup(throws)
    assert [t["throw_id"] for t in kept] == ["a", "mystery"]


def test_dedupe_same_util_no_coords_still_dedupes():
    throws = [
        _throw("a", 1000, x=10, y=20, z=30),
        {"throw_id": "mystery", "throw_tick": 1500, "util_type": "smoke",
         "is_renderable": True, "flight_ticks": 50},
    ]
    kept = dedupe_throws_by_lineup(throws)
    assert [t["throw_id"] for t in kept] == ["a"]


def test_dedupe_keeps_earliest_regardless_of_input_order():
    throws = [
        _throw("late", 2000, x=5000, y=0, z=0),
        _throw("early", 1000, x=10, y=0, z=0),
        _throw("flash", 1500, util="flash", x=0, y=0, z=0),
    ]
    assert [t["throw_id"] for t in dedupe_throws_by_lineup(throws)] == ["early", "flash"]


def test_dedupe_collapses_same_landing_far_release():
    # Real case (donk Mirage con smokes): same landing, releases 270u apart.
    throws = [
        _throw("first", 11129, x=-721, y=-1341, z=-93,
               land_x=29, land_y=-2324, land_z=-38),
        _throw("repeat", 17072, x=-991, y=-1379, z=-91,
               land_x=22, land_y=-2314, land_z=-38),
    ]
    assert [t["throw_id"] for t in dedupe_throws_by_lineup(throws)] == ["first"]


def test_dedupe_collapses_boundary_straddle_releases():
    # Releases 5u apart but straddling a 96u grid boundary collapse by
    # distance (grid cells would split them).
    throws = [
        _throw("a", 30987, x=-678, y=-1156, z=-103,
               land_x=-637, land_y=-732, land_z=-266),
        _throw("b", 67360, x=-682, y=-1151, z=-107,
               land_x=-635, land_y=-745, land_z=-265),
    ]
    assert [t["throw_id"] for t in dedupe_throws_by_lineup(throws)] == ["a"]


def test_dedupe_keeps_different_type_same_spot():
    throws = [
        _throw("smoke", 1000, x=0, y=0, z=0, land_x=100, land_y=100, land_z=0),
        _throw("flash", 2000, util="flash", x=0, y=0, z=0, land_x=100, land_y=100, land_z=0),
    ]
    assert [t["throw_id"] for t in dedupe_throws_by_lineup(throws)] == ["smoke", "flash"]


def test_select_window_throws_prefers_watchable_repeat():
    from overlay.overlay_utilcams import _play_window_for_throw  # noqa
    ranges = {1: (10000, 20000)}
    throws = [
        _throw("cut", 100, x=10, y=0, z=0),      # freeze cut, same lineup...
        _throw("live", 15000, x=15, y=0, z=0),   # ...as this watchable repeat
    ]
    in_window = select_window_throws(throws, ranges)
    assert [t["throw_id"] for t in in_window] == ["live"]
    kept = dedupe_throws_by_lineup(in_window)
    assert [t["throw_id"] for t in kept] == ["live"]
    assert _play_window_for_throw(100, ranges) is None


def test_classify_fails_safe_without_cs2util(tmp_path, monkeypatch):
    _without_cs2util(monkeypatch)
    throws = [_throw("a", 1000), _throw("b", 2000)]
    out = classify_throws_straightforward(throws, data_dir=tmp_path, map_name="de_nuke")
    assert out == {"a": True, "b": True}


def test_split_straightforward_keeps_all_without_data(tmp_path):
    throws = [_throw("a", 1000), _throw("b", 2000)]
    keep, drop = split_straightforward(throws, data_dir=None)
    assert [t["throw_id"] for t in keep] == ["a", "b"]
    assert drop == []
    keep, drop = split_straightforward(throws, data_dir=tmp_path)
    assert [t["throw_id"] for t in keep] == ["a", "b"]
    assert drop == []


def test_intuitive_lob_requires_blocked_los_and_far_exit():
    # The reported HE at 45s: blocked sightline, but the trajectory leaves the
    # thrower's volume 637u away -> long open lob, not a lineup.
    assert is_intuitive_lob(los_open=False, exit_dist=637.0) is True
    # Nearby loft over a wall: leaves the volume almost immediately.
    assert is_intuitive_lob(los_open=False, exit_dist=14.0) is False
    # Open sightline is already handled by classify_throw.
    assert is_intuitive_lob(los_open=True, exit_dist=900.0) is False
    # Boundary is inclusive at LOFT_EXIT_MAX.
    from overlay.lineup_freeze import LOFT_EXIT_MAX
    assert is_intuitive_lob(los_open=False, exit_dist=LOFT_EXIT_MAX) is True
    assert is_intuitive_lob(los_open=False, exit_dist=LOFT_EXIT_MAX - 1.0) is False


def test_expand_offsets_shifts_later_rounds():
    offsets = {1: 0.0, 2: 50.0, 3: 100.0}
    durations = {1: 50.0, 2: 50.0, 3: 60.0}
    frame_ranges = {1: (0, 2999), 2: (3000, 5999), 3: (6000, 9599)}
    new_off, new_dur, windows = expand_offsets_for_freezes(
        offsets, durations, frame_ranges, [(100, 1.5), (4000, 2.5)], fps=60.0,
    )
    assert new_off == {1: 0.0, 2: 51.5, 3: 104.0}
    assert new_dur == {1: 51.5, 2: 52.5, 3: 60.0}
    assert windows == [
        {"frame": 100.0, "hold_seconds": 1.5, "round": 1.0},
        {"frame": 4000.0, "hold_seconds": 2.5, "round": 2.0},
    ]


def test_expand_offsets_no_freezes_passthrough():
    offsets = {1: 0.0}
    durations = {1: 50.0}
    new_off, new_dur, windows = expand_offsets_for_freezes(
        offsets, durations, {1: (0, 2999)}, [], fps=60.0,
    )
    assert (new_off, new_dur, windows) == (offsets, durations, [])
    assert new_off is not offsets  # copies, never mutates caller state


def test_freeze_frame_plan_net_insert_matches_seconds():
    plan = freeze_frame_plan([(100, 1.5), (4000, 2.5)], fps=60.0)
    assert plan["anchors"] == [100, 4000]
    assert plan["hold_frames"] == [90, 150]
    assert plan["prefix"] == [0, 90, 240]
    assert plan["exp_anchors"] == [100, 4090]
    assert plan["total_frames"] == 240


def test_freeze_frame_plan_accepts_window_dicts_and_sorts():
    plan = freeze_frame_plan(
        [{"frame": 4000, "hold_seconds": 2.5}, {"frame": 100, "hold_seconds": 1.5}],
        fps=60.0,
    )
    assert plan["anchors"] == [100, 4000]
    assert plan["total_frames"] == 240


def test_expand_and_collapse_frame_round_trip():
    plan = freeze_frame_plan([(100, 1.5)], fps=60.0)  # 90 inserted frames
    # Before the anchor: untouched.
    assert expand_frame(0, plan) == 0
    assert expand_frame(99, plan) == 99
    # At/after the anchor: pushed past the hold.
    assert expand_frame(100, plan) == 190
    assert expand_frame(599, plan) == 689
    # Inside the hold clamps back to the anchor (keys stay held).
    assert collapse_frame(100, plan) == 100
    assert collapse_frame(189, plan) == 100
    # After the hold inverts exactly.
    assert collapse_frame(190, plan) == 100
    assert collapse_frame(689, plan) == 599
    assert collapse_frame(50, plan) == 50


def test_expand_and_collapse_multiple_holds():
    plan = freeze_frame_plan([(100, 1.5), (300, 1.0)], fps=60.0)  # +90, +60
    assert expand_frame(299, plan) == 389     # only the first hold applies
    assert expand_frame(300, plan) == 450     # second hold applies at anchor
    assert collapse_frame(449, plan) == 300   # inside second hold -> anchor
    assert collapse_frame(450, plan) == 300
    assert collapse_frame(389, plan) == 299


def test_reconstruct_pristine_timeline_inverts_expansion():
    offsets = {1: 0.0, 2: 50.0, 3: 100.0}
    durations = {1: 50.0, 2: 50.0, 3: 60.0}
    frame_ranges = {1: (0, 2999), 2: (3000, 5999), 3: (6000, 9599)}
    new_off, new_dur, windows = expand_offsets_for_freezes(
        offsets, durations, frame_ranges, [(100, 1.5), (4000, 2.5)], fps=60.0,
    )
    got_off, got_dur = reconstruct_pristine_timeline(new_off, new_dur, windows, fps=60.0)
    assert got_off == offsets
    assert got_dur == durations


def test_pip_mapping_stays_put_across_a_freeze():
    """A throw after a freeze must not drift by the inserted hold.

    Reproduces the reported bug: the expanded sidecar stretches the whole
    round, so the naive linear map places a later PiP tens of frames off.
    The pristine map + expand_frame keeps it exact.
    """
    from overlay.overlay_utilcams import _map_throw_tick_to_frame

    rs, re = 10000, 20000          # 10000 ticks of gameplay
    pristine = {1: (0, 599)}       # 600 pristine frames
    expanded = {1: (0, 689)}       # +90 frames from a 1.5s hold at frame 100
    tick_mid = 16000               # 60% through the round
    base = _map_throw_tick_to_frame(tick_mid, {1: (rs, re)}, pristine)
    naive = _map_throw_tick_to_frame(tick_mid, {1: (rs, re)}, expanded)
    plan = freeze_frame_plan([(100, 1.5)], fps=60.0)
    exact = expand_frame(base, plan)
    assert base == 359
    assert exact == 449            # +90 frames, the real hold
    assert naive == 413            # naive stretched map drifts ~36 frames
    assert exact != naive


def _ratings_fixture(path: Path) -> Path:
    p = path / "ratings.json"
    p.write_text(json.dumps({
        "match_stage": "Grand Final",
        "tables": [
            {"team": "Natus Vincere", "map": "Nuke", "players": [
                {"nickname": "Aleksib", "rating": "1.20", "kd": "20-10",
                 "adr": "80.0", "kast": "70.0%"},
            ]},
            {"team": "Team Spirit", "map": "Nuke", "players": [
                {"nickname": "donk", "rating": "1.50", "kd": "25-10",
                 "adr": "95.0", "kast": "75.0%"},
            ]},
        ],
    }))
    return p


def _titlize(ratings: Path, *extra: str) -> dict:
    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "pov" / "generate_title.py"),
         str(ratings), "--player", "Aleksib", "--map", "Nuke",
         "--variant", "overlay", *extra],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_overlay_branding_drops_input_by_default(tmp_path):
    meta = _titlize(_ratings_fixture(tmp_path))
    assert "input overlay" not in meta["tags"]
    assert "keyboard overlay" not in meta["tags"]
    assert "mouse input" not in meta["tags"]
    assert "utility cam" in meta["tags"]
    assert "keyboard" not in meta["description"].lower()


def test_overlay_branding_keeps_input_with_keyboard(tmp_path):
    meta = _titlize(_ratings_fixture(tmp_path), "--keyboard")
    assert "input overlay" in meta["tags"]
    assert "keyboard overlay" in meta["tags"]
    assert "keyboard" in meta["description"].lower()
