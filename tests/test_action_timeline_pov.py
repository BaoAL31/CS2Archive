"""POV action-timeline surface: slice helper, per-demo cache, thumbnail bonus.

Pure tests where possible (synthetic timelines); the cache tests use a fake
renders/ dir via monkeypatched PROJECT_ROOT, never a real demo.
"""
from __future__ import annotations

import json
from pathlib import Path

from cs2archive.highlights import build_action_timeline as atl  # noqa: E402
from thumbnail.utils import kill_bg_bonus  # noqa: E402

SID = "76561199032006224"
OTHER = "76561199378680280"


def _kill(tick, rnd, aid, vid, weapon="ak47", victim_weapon="M4A4", hs=True):
    return {"tick": tick, "round": rnd, "attacker_steam_id": aid,
            "victim_steam_id": vid, "weapon": weapon,
            "victim_weapon": victim_weapon, "headshot": hs}


def _cand(sid, role):
    return {"steam_id": sid, "role": role, "is_pro": True}


def _timeline():
    return {
        "map": "Nuke", "tickrate": 64,
        "kills_all": [
            _kill(11582, 2, SID, OTHER, "deagle", "AK-47"),
            _kill(14702, 2, SID, "v3", "ak47", "AK-47"),
            _kill(14802, 2, OTHER, SID, "m4a1_silencer", "AK-47"),
        ],
        "moments": [
            {"id": "r2-trade-4", "type": "trade", "round": 2,
             "start_tick": 11262, "end_tick": 11710, "kill_ticks": [11582],
             "pov_candidates": [_cand(SID, "attacker"), _cand(OTHER, "victim")]},
            {"id": "r2-opener-1", "type": "opener", "round": 2,
             "start_tick": 11000, "end_tick": 11710, "kill_ticks": [11582],
             "attacker_steam_id": SID},
            {"id": "r2-burst-1", "type": "util_burst", "round": 2,
             "start_tick": 11000, "end_tick": 11710, "kill_ticks": [],
             "pov_candidates": [_cand(SID, "exec")]},
            {"id": "r3-opener-1", "type": "opener", "round": 3,
             "start_tick": 18000, "end_tick": 18400, "kill_ticks": [18236],
             "attacker_steam_id": OTHER},
        ],
        "rounds": [{"round": 2, "stakes": {"buys": {"2": "full", "3": "full"}}}],
    }


# ── pov_action_slice ────────────────────────────────────────────────

def test_slice_kills_and_deaths_by_sid():
    sl = atl.pov_action_slice(_timeline(), SID)
    assert [k["tick"] for k in sl["kills"]] == [11582, 14702]
    assert [k["tick"] for k in sl["deaths"]] == [14802]
    assert sl["map"] == "Nuke"
    assert sl["tickrate"] == 64
    assert len(sl["rounds"]) == 1


def test_slice_moments_require_non_victim_role():
    sl = atl.pov_action_slice(_timeline(), SID)
    ids = [m["id"] for m in sl["moments"]]
    assert "r2-trade-4" in ids  # attacker role
    assert "r2-opener-1" in ids  # attacker_steam_id field
    assert "r2-burst-1" in ids  # exec thrower counts as involved
    assert "r3-opener-1" not in ids  # someone else's opener


def test_slice_victim_only_moment_excluded():
    tl = _timeline()
    tl["moments"] = [{
        "id": "r9-closer-1", "type": "closer", "round": 9,
        "start_tick": 48000, "end_tick": 48700, "kill_ticks": [48552],
        "pov_candidates": [_cand(OTHER, "attacker"), _cand(SID, "victim")]}]
    sl = atl.pov_action_slice(tl, SID)
    assert sl["moments"] == []


def test_slice_unknown_sid_is_empty_not_error():
    sl = atl.pov_action_slice(_timeline(), "000")
    assert sl["kills"] == [] and sl["deaths"] == [] and sl["moments"] == []


# ── ensure_action_timeline (fake renders dir) ───────────────────────

def _fake_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(atl, "PROJECT_ROOT", tmp_path)
    return tmp_path


def test_cache_hit_returns_path_without_build(monkeypatch, tmp_path):
    _fake_renders(monkeypatch, tmp_path)
    out = tmp_path / "renders" / "hl-demo" / "action_timeline.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"timeline_version": atl.TIMELINE_VERSION}))
    assert atl.ensure_action_timeline(Path("demo.dem")) == out


def test_stale_cache_rebuilds_and_warns_on_missing_demo(monkeypatch, tmp_path, capsys):
    _fake_renders(monkeypatch, tmp_path)
    out = tmp_path / "renders" / "hl-demo" / "action_timeline.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"timeline_version": 1}))
    assert atl.ensure_action_timeline(Path("demo.dem")) is None
    assert "WARN" in capsys.readouterr().out


def test_missing_cache_build_failure_returns_none(monkeypatch, tmp_path, capsys):
    _fake_renders(monkeypatch, tmp_path)
    assert atl.ensure_action_timeline(Path("nope.dem")) is None
    assert "WARN" in capsys.readouterr().out


# ── kill_bg_bonus ───────────────────────────────────────────────────

def _stakes(buys):
    return {2: {"buys": buys}}


def test_bonus_full_buy_round():
    k = _kill(11582, 2, SID, OTHER, "deagle", "AK-47")
    assert kill_bg_bonus(k, _stakes({"2": "full", "3": "full"})) == 2 + 1 + 2


def test_bonus_eco_round_scores_no_stakes():
    k = _kill(11582, 2, SID, OTHER, "deagle", "AK-47")
    assert kill_bg_bonus(k, _stakes({"2": "eco", "3": "full"})) == 0 + 1 + 2


def test_bonus_rifle_kill_no_punch_up():
    k = _kill(14702, 2, SID, OTHER, "ak47", "AK-47")
    assert kill_bg_bonus(k, _stakes({"2": "full", "3": "full"})) == 2 + 1 + 0


def test_bonus_body_shot_no_headshot():
    k = _kill(14702, 2, SID, OTHER, "ak47", "AK-47", hs=False)
    assert kill_bg_bonus(k, _stakes({"2": "full", "3": "full"})) == 2 + 0 + 0


def test_bonus_unknown_round_is_zero():
    k = _kill(14702, 2, SID, OTHER, "ak47", "AK-47", hs=False)
    assert kill_bg_bonus(k, {}) == 0
