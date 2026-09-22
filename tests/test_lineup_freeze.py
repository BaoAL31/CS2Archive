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


@pytest.fixture(autouse=True)
def _no_cs2util(monkeypatch):
    import overlay.lineup_freeze as lf
    monkeypatch.setattr(lf, "_cs2util", lambda name: None)


def _throw(tid, tick, util="smoke", x=0.0, y=0.0, z=0.0, **kw):
    row = {
        "throw_id": tid,
        "throw_tick": tick,
        "util_type": util,
        "release_x": x,
        "release_y": y,
        "release_z": z,
        "is_renderable": True,
        "flight_ticks": 100,
    }
    row.update(kw)
    return row


def test_dedupe_keeps_earliest_per_release_cell():
    throws = [
        _throw("a", 1000, x=10, y=20, z=30),
        _throw("b", 2000, x=15, y=25, z=32),  # same 96u cell as a
        _throw("c", 3000, x=5000, y=20, z=30),  # far cell
        _throw("d", 4000, util="flash", x=10, y=20, z=30),  # other type: separate
    ]
    kept = dedupe_throws_by_lineup(throws)
    assert [t["throw_id"] for t in kept] == ["a", "c", "d"]


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
        {"throw_id": "mystery", "throw_tick": 1500, "util_type": "smoke",
         "is_renderable": True, "flight_ticks": 50},
    ]
    kept = dedupe_throws_by_lineup(throws)
    assert [t["throw_id"] for t in kept] == ["a", "mystery"]


def test_dedupe_sorts_by_throw_tick():
    throws = [
        _throw("b", 2000, x=5000, y=0, z=0),
        _throw("a", 1000, x=10, y=0, z=0),
    ]
    assert [t["throw_id"] for t in dedupe_throws_by_lineup(throws)] == ["a", "b"]


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


def test_classify_fails_safe_without_cs2util(tmp_path):
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
