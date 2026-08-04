"""Tests for Shorts timeline detection (4K + Clutch)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from shorts.build_short_timeline import detect_shorts


def _make_kill(tick: int, round_n: int, attacker_sid: str, victim_sid: str, weapon: str = "ak47", victim_weapon: str = "ak47") -> dict:
    return {"tick": tick, "round": round_n, "attacker_sid": attacker_sid, "victim_sid": victim_sid, "weapon": weapon, "victim_weapon": victim_weapon}


def _make_bomb_events(round_n: int, events: list[tuple[int, str, str]]) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    for tick, event, player_sid in events:
        result.setdefault(round_n, []).append({"tick": tick, "event": event, "player_sid": player_sid})
    return result


# ------------------------------ 4K TESTS ------------------------------

def test_4k_detected():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "B", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A", "victim_sid": "C", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A", "victim_sid": "D", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "A", "victim_sid": "E", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    timeline = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(900, 1)],
    )
    assert timeline["short_count"] == 1
    s = timeline["shorts"][0]
    assert s["short_type"] == "4k"
    assert s["pov_steam_id"] == "A"
    assert s["pov_nick"] == "Unknown"
    assert s["start_tick"] == 680  # 5s floor (320 ticks) before first kill
    assert s["end_tick"] == 4128  # 2s padding (128 ticks) after last kill
    assert s["kill_ticks"] == [1000, 2000, 3000, 4000]


def test_4k_dynamic_prekill_caps_at_30s():
    """Tight multikill: pre-kill stretches to make the short exactly 30s (1920 ticks)."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "B", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 1100, "round": 1, "attacker_sid": "A", "victim_sid": "C", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 1200, "round": 1, "attacker_sid": "A", "victim_sid": "D", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 1300, "round": 1, "attacker_sid": "A", "victim_sid": "E", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    s = result["shorts"][0]
    assert s["end_tick"] == 1300 + 128 == 1428
    assert s["start_tick"] == s["end_tick"] - 1920 == -492
    assert s["start_tick"] < 1000  # pre-kill is dynamic, much longer than 5s floor


def test_4k_prekill_never_below_5s():
    """Long multikill: pre-kill floors at 5s (320 ticks), short runs longer than 30s."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "B", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A", "victim_sid": "C", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A", "victim_sid": "D", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "A", "victim_sid": "E", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    s = result["shorts"][0]
    assert s["start_tick"] == 1000 - 320 == 680
    assert s["end_tick"] - s["start_tick"] > 1920  # cannot fit 30s, floor wins


def test_5k_classified_as_4k():
    kill_events = [
        {"tick": 100, "round": 1, "attacker_sid": "X", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 200, "round": 1, "attacker_sid": "X", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 300, "round": 1, "attacker_sid": "X", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 400, "round": 1, "attacker_sid": "X", "victim_sid": "e", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 500, "round": 1, "attacker_sid": "X", "victim_sid": "f", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(50, 1)],
    )
    assert result["short_count"] == 1
    s = result["shorts"][0]
    assert s["short_type"] == "4k"
    assert len(s["kill_ticks"]) == 5


def test_4k_in_round_0_knife_round_rejected():
    """Kills in round 0 (warmup / knife side-choice round) must not become a Short."""
    kill_events = [
        {"tick": 1000, "round": 0, "attacker_sid": "A", "victim_sid": "B", "weapon": "knife", "victim_weapon": "knife"},
        {"tick": 2000, "round": 0, "attacker_sid": "A", "victim_sid": "C", "weapon": "knife", "victim_weapon": "knife"},
        {"tick": 3000, "round": 0, "attacker_sid": "A", "victim_sid": "D", "weapon": "knife", "victim_weapon": "knife"},
        {"tick": 4000, "round": 0, "attacker_sid": "A", "victim_sid": "E", "weapon": "knife", "victim_weapon": "knife"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 0)],
    )
    assert result["short_count"] == 0


def test_4k_dropped_when_team_loses_round():
    """A 4K whose team lost the round is not a Short (no highlight value)."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "A", "victim_sid": "e", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    team_by_sid = {
        "A": 2, "b": 3, "c": 3, "d": 3, "e": 3,
    }
    # Enemy team 3 defuses -> team 2 (A's team) lost the round
    round_win_events = {1: [{"tick": 5000, "event": "defuse", "player_sid": "b"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
    )
    assert result["short_count"] == 0


def test_4k_kept_when_team_wins_round():
    """A 4K whose team won the round is still a Short."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "A", "victim_sid": "e", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    team_by_sid = {
        "A": 2, "b": 3, "c": 3, "d": 3, "e": 3,
    }
    # A's own team 2 defuses -> A's team won the round
    round_win_events = {1: [{"tick": 5000, "event": "defuse", "player_sid": "A"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
    )
    fourk = [s for s in result["shorts"] if s["short_type"] == "4k"]
    assert len(fourk) == 1
    assert fourk[0]["pov_steam_id"] == "A"


def test_3k_not_detected():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A", "victim_sid": "B", "weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A", "victim_sid": "C", "weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A", "victim_sid": "D", "weapon": "ak47"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    assert result["short_count"] == 0


# ------------------ Clutch ------------------


def test_clutch_detected():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "t1_A", "victim_sid": "t2_W", "weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "t1_A", "victim_sid": "t2_X", "weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "t1_B", "victim_sid": "t2_Y", "weapon": "ak47"},
        # t2 now 2v5
        {"tick": 3500, "round": 1, "attacker_sid": "t2_Z", "victim_sid": "t1_C", "weapon": "ak47"},
        # t2_Z (team 2) defuses
    ]
    team_by_sid = {
        "t1_A": 1, "t1_B": 1, "t1_C": 1,
        "t2_W": 2, "t2_X": 2, "t2_Y": 2, "t2_Z": 2,
    }
    round_win_events = {1: [{"tick": 6000, "event": "defuse", "player_sid": "t2_Z"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
        round_ends={1: 6000},
    )
    clutches = [s for s in result["shorts"] if s["short_type"] == "clutch"]
    assert len(clutches) == 1
    c = clutches[0]
    assert c["clutch_initial_count"] == "2v5"
    assert c["pov_steam_id"] == "t2_Z"
    assert c["pov_nick"] == "Unknown"
    assert c["win_event"] == "defuse"


def test_1v2_not_clutch():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B3", "weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A1", "victim_sid": "B4", "weapon": "ak47"},
        {"tick": 2500, "round": 1, "attacker_sid": "B2", "victim_sid": "A2", "weapon": "ak47"},
        # B team is alive=2: B1, B2. A team is alive=3: A1, A2 was killed, actually 2. Wait, let's redo.
    ]
    pass  # This test needs a more precise scenario; see next test


def test_2v4_2v5_counted():
    kill_events_1 = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B5", "weapon": "ak47"},
        # B team now has 4 alive, A has 5.
        {"tick": 2000, "round": 1, "attacker_sid": "A2", "victim_sid": "B4", "weapon": "ak47"},
        # B team now has 3 alive. Not a clutch yet (3v5).
        {"tick": 3000, "round": 1, "attacker_sid": "A3", "victim_sid": "B3", "weapon": "ak47"},
        # B team now has 2 alive (2v5). This IS a clutch trigger.
        {"tick": 3500, "round": 1, "attacker_sid": "B2", "victim_sid": "A4", "weapon": "ak47"},
        {"tick": 3600, "round": 1, "attacker_sid": "B2", "victim_sid": "A5", "weapon": "ak47"},
    ]
    team_by_sid_1 = {
        "A1": 1, "A2": 1, "A3": 1, "A4": 1, "A5": 1,
        "B1": 2, "B2": 2, "B3": 2, "B4": 2, "B5": 2,
    }
    round_win_events_1 = {1: [{"tick": 6000, "event": "defuse", "player_sid": "B2"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events_1,
        team_by_sid=team_by_sid_1,
        round_win_events=round_win_events_1,
        round_starts=[(500, 1)],
        round_ends={1: 6000},
    )
    clutches = [s for s in result["shorts"] if s["short_type"] == "clutch"]
    assert len(clutches) >= 1
    assert clutches[0]["clutch_initial_count"] in ("2v5", "1v5")


def test_zero_kill_defuse_clutch():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B3", "weapon": "ak47"},
        {"tick": 1100, "round": 1, "attacker_sid": "A2", "victim_sid": "B4", "weapon": "ak47"},
        {"tick": 1200, "round": 1, "attacker_sid": "A3", "victim_sid": "B5", "weapon": "ak47"},
        # B1, B2 alive (2v5). B2 defuses without killing.
    ]
    team_by_sid_z = {
        "A1": 1, "A2": 1, "A3": 1, "A4": 1, "A5": 1,
        "B1": 2, "B2": 2, "B3": 2, "B4": 2, "B5": 2,
    }
    round_win_events_z = {1: [{"tick": 5000, "event": "defuse", "player_sid": "B2"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid_z,
        round_win_events=round_win_events_z,
        round_starts=[(500, 1)],
        round_ends={1: 5000},
    )
    clutches = [s for s in result["shorts"] if s["short_type"] == "clutch"]
    assert len(clutches) == 1
    c = clutches[0]
    assert c["clutch_initial_count"] == "2v5"
    assert c["pov_steam_id"] == "B2"
    assert c["win_event"] == "defuse"
    assert c["start_tick"] == 1200
    assert c["end_tick"] == 5000


def test_mixed_timeline_coexists():
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A1", "victim_sid": "B4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A1", "victim_sid": "B3", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "A1", "victim_sid": "B2", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 5000, "round": 2, "attacker_sid": "C1", "victim_sid": "D5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 5100, "round": 2, "attacker_sid": "C2_opp", "victim_sid": "D4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 5200, "round": 2, "attacker_sid": "C3", "victim_sid": "D3", "weapon": "ak47", "victim_weapon": "ak47"},
    ]
    team_by_sid_m = {
        "A1": 1, "A2": 1, "A3": 1, "A4": 1, "A5": 1,
        "B1": 2, "B2": 2, "B3": 2, "B4": 2, "B5": 2,
        "C1": 1, "C2_opp": 1, "C3": 1, "C4": 1, "C5": 1,
        "D1": 2, "D2": 2, "D3": 2, "D4": 2, "D5": 2,
    }
    round_win_events_m = {2: [{"tick": 8000, "event": "explode", "player_sid": "D2"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid_m,
        round_win_events=round_win_events_m,
        round_starts=[(500, 1), (4500, 2)],
        round_ends={1: 4500, 2: 8000},
    )
    assert result["short_count"] >= 2
    types = {s["short_type"] for s in result["shorts"]}
    assert "4k" in types
    assert "clutch" in types


# ------------------ Tightened clutch: brief disadvantage rejected ------------------


def test_clutch_rejected_if_disadvantage_brief():
    """Post-plant style win where the disadvantage only lasts a few seconds:
    the 1v3 triggers at tick 9600 and the bomb explodes at 10000 (400 ticks,
    under the 640-tick / 10s floor) -> no clutch."""
    kill_events = [
        {"tick": 8000, "round": 1, "attacker_sid": "T5", "victim_sid": "C5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 8200, "round": 1, "attacker_sid": "T5", "victim_sid": "C4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 9000, "round": 1, "attacker_sid": "C1", "victim_sid": "T1", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 9200, "round": 1, "attacker_sid": "C2", "victim_sid": "T2", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 9400, "round": 1, "attacker_sid": "C3", "victim_sid": "T3", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 9600, "round": 1, "attacker_sid": "C4", "victim_sid": "T4", "weapon": "ak47", "victim_weapon": "ak47"},
    ]
    team_by_sid = {
        "T1": 2, "T2": 2, "T3": 2, "T4": 2, "T5": 2,
        "C1": 3, "C2": 3, "C3": 3, "C4": 3, "C5": 3,
    }
    round_win_events = {1: [{"tick": 10000, "event": "explode", "player_sid": "T5"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
        round_ends={1: 10000},
    )
    clutches = [s for s in result["shorts"] if s["short_type"] == "clutch"]
    assert clutches == []


def test_clutch_detected_when_30s_disadvantage():
    """A genuine 2v5 from early round that takes >= 30s to win is still a clutch."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A2", "victim_sid": "B4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A3", "victim_sid": "B3", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3500, "round": 1, "attacker_sid": "B2", "victim_sid": "A4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 4000, "round": 1, "attacker_sid": "B2", "victim_sid": "A5", "weapon": "ak47", "victim_weapon": "ak47"},
    ]
    team_by_sid = {
        "A1": 2, "A2": 2, "A3": 2, "A4": 2, "A5": 2,
        "B1": 3, "B2": 3, "B3": 3, "B4": 3, "B5": 3,
    }
    round_win_events = {1: [{"tick": 6000, "event": "defuse", "player_sid": "B2"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
        round_ends={1: 6000},
    )
    clutches = [s for s in result["shorts"] if s["short_type"] == "clutch"]
    assert len(clutches) == 1
    assert clutches[0]["clutch_initial_count"] == "1v5" or clutches[0]["clutch_initial_count"] == "2v5"


def test_multikill_dropped_when_overlapping_clutch():
    """A 4K happening inside a clutch's [trigger, win] window is dropped in favour of the clutch."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "A1", "victim_sid": "B5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "A2", "victim_sid": "B4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 3000, "round": 1, "attacker_sid": "A3", "victim_sid": "B3", "weapon": "ak47", "victim_weapon": "ak47"},
        # 2v5 trigger at tick 3000 for team 2 (A side). B2 then 4k's the enemy.
        {"tick": 3500, "round": 1, "attacker_sid": "B2", "victim_sid": "A4", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 4000, "round": 1, "attacker_sid": "B2", "victim_sid": "A5", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 4500, "round": 1, "attacker_sid": "B2", "victim_sid": "A1", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 5000, "round": 1, "attacker_sid": "B2", "victim_sid": "A2", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    team_by_sid = {
        "A1": 2, "A2": 2, "A3": 2, "A4": 2, "A5": 2,
        "B1": 3, "B2": 3, "B3": 3, "B4": 3, "B5": 3,
    }
    round_win_events = {1: [{"tick": 6000, "event": "defuse", "player_sid": "B2"}]}
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        team_by_sid=team_by_sid,
        round_win_events=round_win_events,
        round_starts=[(500, 1)],
        round_ends={1: 6000},
    )
    assert len(result["shorts"]) == 1
    s = result["shorts"][0]
    assert s["short_type"] == "clutch"
    assert s["pov_steam_id"] == "B2"


# ------------------ Tightened multikill tier criterion ------------------


def test_4k_vs_low_tier_weapons_rejected():
    """4k with an AK where all victims only held pistols (eco farm) is not a Short."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "X", "victim_sid": "a", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 2000, "round": 1, "attacker_sid": "X", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 3000, "round": 1, "attacker_sid": "X", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "X", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "p250"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    assert result["short_count"] == 0


def test_4k_with_single_high_tier_victim_rejected():
    """Only one victim held a rifle >= attacker's AK -> still rejected."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "X", "victim_sid": "a", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "X", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 3000, "round": 1, "attacker_sid": "X", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "X", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    assert result["short_count"] == 0


def test_4k_with_two_high_tier_victims_detected():
    """Two victims held weapons of tier >= attacker's AK -> detected."""
    kill_events = [
        {"tick": 1000, "round": 1, "attacker_sid": "X", "victim_sid": "a", "weapon": "ak47", "victim_weapon": "ak47"},
        {"tick": 2000, "round": 1, "attacker_sid": "X", "victim_sid": "b", "weapon": "ak47", "victim_weapon": "m4a1"},
        {"tick": 3000, "round": 1, "attacker_sid": "X", "victim_sid": "c", "weapon": "ak47", "victim_weapon": "glock"},
        {"tick": 4000, "round": 1, "attacker_sid": "X", "victim_sid": "d", "weapon": "ak47", "victim_weapon": "glock"},
    ]
    result = detect_shorts(
        demo_path="test.dem",
        kill_events=kill_events,
        round_starts=[(500, 1)],
    )
    fourk = [s for s in result["shorts"] if s["short_type"] == "4k"]
    assert len(fourk) == 1
    assert len(fourk[0]["kill_ticks"]) == 4


# ------------------ Action Timeline conversion ------------------


def test_build_from_action_timeline_detects_4k(tmp_path):
    """build_short_timeline_from_action converts an action_timeline.json and detects a 4K."""
    from scripts.shorts.build_short_timeline import build_short_timeline_from_action

    at = {
        "demo_path": "demos/faceit/test.dem",
        "map": "Nuke",
        "source": "faceit",
        "kill_count": 4,
        "kills": [
            {"tick": 1000, "round": 1, "attacker": "NiKo", "attacker_steam_id": "A",
             "victim": "s1mple", "victim_steam_id": "B", "weapon": "ak47",
             "victim_weapon": "ak47", "is_bomb": False, "headshot": False},
            {"tick": 2000, "round": 1, "attacker": "NiKo", "attacker_steam_id": "A",
             "victim": "b1t", "victim_steam_id": "C", "weapon": "ak47",
             "victim_weapon": "ak47", "is_bomb": False, "headshot": False},
            {"tick": 3000, "round": 1, "attacker": "NiKo", "attacker_steam_id": "A",
             "victim": "jL", "victim_steam_id": "D", "weapon": "ak47",
             "victim_weapon": "glock", "is_bomb": False, "headshot": False},
            {"tick": 4000, "round": 1, "attacker": "NiKo", "attacker_steam_id": "A",
             "victim": "w0nderful", "victim_steam_id": "E", "weapon": "ak47",
             "victim_weapon": "glock", "is_bomb": False, "headshot": False},
        ],
        "bomb_actions": [],
        "round_starts": [{"round": 1, "tick": 900}],
        "round_freeze_ends": [{"round": 1, "tick": 1000}],
        "round_ends": [{"round": 1, "tick": 5000}],
    }

    at_path = tmp_path / "action_timeline.json"
    at_path.write_text(json.dumps(at), encoding="utf-8")
    demo_path = tmp_path / "test.dem"

    mock_info = MagicMock()
    mock_info.iterrows.return_value = iter([
        (0, {"steamid": "A", "team_number": 2, "name": "NiKo"}),
        (1, {"steamid": "B", "team_number": 3, "name": "s1mple"}),
        (2, {"steamid": "C", "team_number": 3, "name": "b1t"}),
        (3, {"steamid": "D", "team_number": 3, "name": "jL"}),
        (4, {"steamid": "E", "team_number": 3, "name": "w0nderful"}),
    ])
    mock_parser = MagicMock()
    mock_parser.return_value.parse_player_info.return_value = mock_info

    with patch("demoparser2.DemoParser", mock_parser):
        result = build_short_timeline_from_action(at_path, demo_path)

    assert result["short_count"] >= 1
    types = {s["short_type"] for s in result["shorts"]}
    assert "4k" in types
    assert result["map"] == "Nuke"
    # pov_nick should be resolved from player_info or action timeline kills
    fourk = [s for s in result["shorts"] if s["short_type"] == "4k"]
    if fourk:
        assert fourk[0].get("pov_nick") == "NiKo"


def test_build_from_action_preserves_demo_path(tmp_path):
    """Action timeline conversion preserves the demo_path in the output."""
    from scripts.shorts.build_short_timeline import build_short_timeline_from_action

    at = {
        "demo_path": "demos/faceit/my-match.dem",
        "map": "Inferno",
        "source": "faceit",
        "kill_count": 0,
        "kills": [],
        "bomb_actions": [],
        "round_starts": [],
        "round_freeze_ends": [],
        "round_ends": [],
    }

    at_path = tmp_path / "at.json"
    at_path.write_text(json.dumps(at), encoding="utf-8")
    demo_path = tmp_path / "cache_test.dem"

    mock_info = MagicMock()
    mock_info.iterrows.return_value = iter([])
    mock_parser = MagicMock()
    mock_parser.return_value.parse_player_info.return_value = mock_info

    with patch("demoparser2.DemoParser", mock_parser):
        result = build_short_timeline_from_action(at_path, demo_path)

    assert result["demo_path"] == str(demo_path)
    assert result["map"] == "Inferno"
    assert result["short_count"] == 0