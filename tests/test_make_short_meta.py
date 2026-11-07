"""Title length + hand-edit preservation for shorts meta."""
from __future__ import annotations

import json
from pathlib import Path

import cs2archive.shorts.make_short_meta as mm


def test_fit_title_trims_map_tag_first():
    base = "ridiculous 4K from flameZ vs Falcons #cs2 #counterstrike"
    out = mm._fit_title(base, "#eslproleague2026", "#flamez #vitality #falcons #dust2")
    assert len(out) <= 100
    assert out.startswith(base)
    assert "#dust2" not in out
    assert "#flamez" in out


def test_fit_title_keeps_short_titles_whole():
    out = mm._fit_title("4K from flameZ vs Falcons #cs2 #counterstrike",
                        "#eslproleague2026", "#flamez #falcons")
    assert out == ("4K from flameZ vs Falcons #cs2 #counterstrike "
                   "#eslproleague2026 #flamez #falcons")


def test_fit_title_raises_on_pathological_base():
    import pytest
    with pytest.raises(ValueError):
        mm._fit_title("x" * 101, "", "")


def _timeline(tmp_path: Path) -> Path:
    folder = tmp_path / "shorts-x"
    folder.mkdir()
    (folder / "short_timeline.json").write_text(json.dumps({
        "demo_path": "demos/hltv/x/y.dem",
        "map": "de_dust2",
        "shorts": [{
            "short_type": "4k",
            "pov_steam_id": "1",
            "pov_nick": "flameZ",
            "kill_ticks": [1, 2, 3, 4],
            "start_tick": 0,
        }],
    }), encoding="utf-8")
    return folder


def test_make_meta_preserves_hand_edited_title(tmp_path: Path, monkeypatch):
    folder = _timeline(tmp_path)
    monkeypatch.setattr(mm, "detect_pov_opponent", lambda *a: ("Vitality", "Falcons"))
    meta = mm.make_meta(str(folder))
    assert len(meta["title"]) <= 100
    hand = "4K from flameZ vs Falcons #cs2 #counterstrike"
    prev = dict(meta, title=hand, title_format="custom")
    (folder / "upload_meta_shorts.json").write_text(json.dumps(prev), encoding="utf-8")
    meta2 = mm.make_meta(str(folder))
    assert meta2["title"] == hand
    assert meta2["title_format"] == "custom"


def test_make_meta_regenerates_blank_title(tmp_path: Path, monkeypatch):
    folder = _timeline(tmp_path)
    monkeypatch.setattr(mm, "detect_pov_opponent", lambda *a: ("Vitality", "Falcons"))
    (folder / "upload_meta_shorts.json").write_text(
        json.dumps({"title": "", "title_format": ""}), encoding="utf-8")
    meta = mm.make_meta(str(folder))
    assert meta["title"]
    assert len(meta["title"]) <= 100
