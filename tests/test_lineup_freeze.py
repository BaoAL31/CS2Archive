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
