"""Obvious-defuse trim (last-action anchor) + dissolve margin planning."""

from __future__ import annotations

from cs2archive.pov.round_windows import (
    _obvious_defuse_cut_end,
    plan_round_windows,
)
from cs2archive.pov.save_transition import FADE_SECONDS, plan_dissolves

POV = "76561199032006224"
MATE = "76561198182116403"
T1 = "76561198300934672"
T2 = "76561198875356056"


def _data(**over) -> dict:
    d = {
        "teamA": {"name": "CTs"},
        "teamB": {"name": "Ts"},
        "players": [
            {"steamId": POV, "teamName": "CTs"},
            {"steamId": MATE, "teamName": "CTs"},
            {"steamId": T1, "teamName": "Ts"},
            {"steamId": T2, "teamName": "Ts"},
        ],
        "rounds": [{
            "number": 9, "startTick": 60829, "endTick": 69267,
            "winnerSide": 3, "winnerTeamName": "CTs", "endReason": 7,
            "teamASide": 3, "teamBSide": 2, "freezetimeEndTick": 62109,
        }],
        "kills": [
            {"roundNumber": 9, "tick": 68000,
             "killerName": "m", "victimName": "t1",
             "killerSteamId": MATE, "victimSteamId": T1},
            {"roundNumber": 9, "tick": 68435,
             "killerName": "pov", "victimName": "t2",
             "killerSteamId": POV, "victimSteamId": T2},
        ],
        "shots": [
            {"roundNumber": 9, "tick": 68400, "playerSteamId": POV},
        ],
        "damages": [],
        "grenadeDestroyed": [],
        "bombsPlanted": [{"roundNumber": 9, "tick": 67243}],
        "bombsDefused": [{"roundNumber": 9, "tick": 69267}],
    }
    d.update(over)
    return d


def _cut(data: dict) -> int | None:
    return _obvious_defuse_cut_end(
        data, data["rounds"][0], data["kills"], 9, 69267, POV)


def test_cut_anchors_at_last_action_plus_5s():
    # last team action 68435 (kill) -> cut 68435 + 320 = 68755
    assert _cut(_data()) == 68435 + 320


def test_fallback_to_3s_before_defuse_without_team_action():
    d = _data(shots=[], kills=[
        {"roundNumber": 9, "tick": 68000,
         "killerName": "t2", "victimName": "t1",
         "killerSteamId": T2, "victimSteamId": T1},
        {"roundNumber": 9, "tick": 68435,
         "killerName": "t1", "victimName": "t2",
         "killerSteamId": T1, "victimSteamId": T2},
    ])
    assert _cut(d) == 69267 - 192


def test_last_second_defuse_kept_whole():
    d = _data(bombsDefused=[{"roundNumber": 9, "tick": 69743}])
    # 2624 - (69743 - 67243) = 124 < 128 -> not obvious
    assert _cut(d) is None


def test_live_t_kept_whole():
    d = _data(kills=[{
        "roundNumber": 9, "tick": 68000,
        "killerName": "m", "victimName": "t1",
        "killerSteamId": MATE, "victimSteamId": T1,
    }])
    assert _cut(d) is None


def test_pov_died_kept_whole():
    d = _data(kills=_data()["kills"] + [{
        "roundNumber": 9, "tick": 68100,
        "killerName": "t1", "victimName": "pov",
        "killerSteamId": T1, "victimSteamId": POV,
    }])
    assert _cut(d) is None


def test_plan_round_windows_trims_decided_defuse():
    windows = plan_round_windows(_data(), steam_id=POV)
    assert len(windows) == 1
    w = windows[0]
    assert w.trimmed and "obvious defuse trim" in w.reason
    assert (w.start_tick, w.end_tick) == (61981, 68435 + 320)


def test_plan_dissolves_keeps_margined_junctions():
    assert plan_dissolves(100.0, [10.0, 50.0], FADE_SECONDS) == [10.0, 50.0]


def test_plan_dissolves_drops_edge_and_crowded_junctions():
    assert plan_dissolves(100.0, [0.5, 10.0, 99.5], FADE_SECONDS) == [10.0]
    assert plan_dissolves(100.0, [10.0, 10.5], FADE_SECONDS) == [10.0]
    assert plan_dissolves(100.0, [], FADE_SECONDS) == []
