"""Pro multi-kill must not split on a single sandwiched kill (e.g. trade then molly)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _fix_edit_timeline,
    _merge_pro_runs_through_solo_interrupts,
)


def test_merge_pro_runs_through_solo_interrupt():
    pro = "76561198044045107"
    other = "76561197996678278"
    kills = [
        {"tick": 100, "round": 1, "attacker_steam_id": pro},
        {"tick": 110, "round": 1, "attacker_steam_id": other},
        {"tick": 120, "round": 1, "attacker_steam_id": pro},
    ]
    runs = [(pro, [0]), (other, [1]), (pro, [2])]
    merged = _merge_pro_runs_through_solo_interrupts(runs, kills, {pro})
    assert merged == [(pro, [0, 2])]


def test_fix_merges_electronic_trade_molly_pattern():
    pro = "76561198044045107"
    teses = "76561197996678278"
    action = {
        "kills": [
            {"tick": 49638, "round": 9, "attacker": "electroNic", "attacker_steam_id": pro,
             "victim": "x", "victim_steam_id": "1", "weapon": "m4a1", "is_bomb": False, "headshot": False},
            {"tick": 49678, "round": 9, "attacker": "TeSeS", "attacker_steam_id": teses,
             "victim": "electroNic", "victim_steam_id": pro, "weapon": "ak47", "is_bomb": False, "headshot": True},
            {"tick": 49686, "round": 9, "attacker": "electroNic", "attacker_steam_id": pro,
             "victim": "TeSeS", "victim_steam_id": teses, "weapon": "inferno", "is_bomb": False, "headshot": False},
        ],
        "round_starts": [{"round": 9, "tick": 40000}],
        "round_freeze_ends": [{"round": 9, "tick": 41500}],
    }
    players = {pro: "electroNic", teses: "TeSeS", "1": "x"}
    llm = {
        "segments": [
            {"start_tick": 49000, "end_tick": 50000, "pov_steam_id": pro, "segment_type": "default",
             "kill_indices": [0], "rationale": "a"},
            {"start_tick": 49000, "end_tick": 50000, "pov_steam_id": teses, "segment_type": "default",
             "kill_indices": [1], "rationale": "b"},
            {"start_tick": 49000, "end_tick": 50000, "pov_steam_id": pro, "segment_type": "default",
             "kill_indices": [2], "rationale": "c"},
        ],
    }
    fixed = _fix_edit_timeline(llm, action, players)["segments"]
    round9 = [s for s in fixed if set(s["kill_indices"]) <= {0, 1, 2}]
    assert len(round9) == 1
    assert sorted(round9[0]["kill_indices"]) == [0, 2]
    assert round9[0]["pov_steam_id"] == pro


def test_fix_drops_nonpro_solo_keeps_nonpro_multi():
    pro = "76561198044045107"
    rando = "76561197990000001"
    action = {
        "kills": [
            {"tick": 1000, "round": 1, "attacker": "Rando", "attacker_steam_id": rando,
             "victim": "pro", "victim_steam_id": pro, "weapon": "ak47", "is_bomb": False, "headshot": False},
            {"tick": 5000, "round": 2, "attacker": "Rando", "attacker_steam_id": rando,
             "victim": "a", "victim_steam_id": "2", "weapon": "ak47", "is_bomb": False, "headshot": False},
            {"tick": 5100, "round": 2, "attacker": "Rando", "attacker_steam_id": rando,
             "victim": "b", "victim_steam_id": "3", "weapon": "ak47", "is_bomb": False, "headshot": False},
            {"tick": 8000, "round": 3, "attacker": "electroNic", "attacker_steam_id": pro,
             "victim": "x", "victim_steam_id": "4", "weapon": "ak47", "is_bomb": False, "headshot": False},
        ],
        "round_starts": [
            {"round": 1, "tick": 0},
            {"round": 2, "tick": 4000},
            {"round": 3, "tick": 7000},
        ],
        "round_freeze_ends": [
            {"round": 1, "tick": 100},
            {"round": 2, "tick": 4100},
            {"round": 3, "tick": 7100},
        ],
    }
    players = {pro: "electroNic", rando: "Rando", "2": "a", "3": "b", "4": "x"}
    llm = {
        "segments": [
            {"start_tick": 900, "end_tick": 1400, "pov_steam_id": rando, "segment_type": "default",
             "kill_indices": [0], "rationale": "rando 1k"},
            {"start_tick": 4900, "end_tick": 5400, "pov_steam_id": rando, "segment_type": "multi_kill",
             "kill_indices": [1, 2], "rationale": "rando 2k"},
            {"start_tick": 7900, "end_tick": 8400, "pov_steam_id": pro, "segment_type": "default",
             "kill_indices": [3], "rationale": "pro 1k ok"},
        ],
    }
    fixed = _fix_edit_timeline(llm, action, players)["segments"]
    by_kis = {frozenset(s["kill_indices"]): s for s in fixed}
    assert frozenset([0]) not in by_kis
    assert frozenset([1, 2]) in by_kis
    assert by_kis[frozenset([1, 2])]["pov_steam_id"] == rando
    assert frozenset([3]) in by_kis
    assert by_kis[frozenset([3])]["pov_steam_id"] == pro
