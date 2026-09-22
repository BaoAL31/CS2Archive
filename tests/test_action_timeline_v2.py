"""Timeline v2: deterministic moments (_derive_moments) on synthetic data.

No demo needed — pure function in, moments out. Covers multikill, clutch
(won + attempt), CT clock exclusion, opener, trade, bomb plays, and POV
candidate ranking.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_action_timeline import (  # noqa: E402
    _derive_moments,
    _pov_candidates,
)

PRO = "76561198000000001"
MATE = "76561198000000002"
RANDO = "76561198000000003"
E1 = "76561198000000004"
E2 = "76561198000000005"
E3 = "76561198000000006"
E4 = "76561198000000007"

TEAMS = {PRO: 2, MATE: 2, RANDO: 2, E1: 3, E2: 3, E3: 3, E4: 3}
PRO_SIDS = {PRO: "ProPlayer", MATE: "ProMate"}

# Full 5v5 rosters (alive counts start 5v5, like real FACEIT demos).
T2 = [PRO, MATE, RANDO, "76561198000000008", "76561198000000009"]
T3 = [E1, E2, E3, E4, "76561198000000010"]
TEAMS_5V5 = {s: 2 for s in T2} | {s: 3 for s in T3}


def _kill(tick, rnd, aid, vid, weapon="ak47", victim_weapon="ak47"):
    return {
        "tick": tick, "round": rnd,
        "attacker": aid, "attacker_steam_id": aid,
        "victim": vid, "victim_steam_id": vid,
        "weapon": weapon, "victim_weapon": victim_weapon,
        "is_bomb": False, "headshot": False,
        "attacker_is_pro": aid in PRO_SIDS,
        "victim_is_pro": vid in PRO_SIDS,
    }


def _base(**over):
    d = dict(
        kills_all=[],
        round_deaths={},
        round_starts={1: 1000},
        round_ends={1: 9000},
        winner_by_round={1: 2},
        win_reason_by_round={1: "t_killed"},
        bomb_actions=[],
        team_by_sid=dict(TEAMS),
        pro_sids=dict(PRO_SIDS),
    )
    d.update(over)
    return d


def _run(**over):
    return _derive_moments(**_base(**over))


def test_multikill_ranks_pro_attacker_first():
    victims = [E1, E2, E3]
    ks = [_kill(2000 + n * 100, 1, PRO, v) for n, v in enumerate(victims)]
    ms = _run(kills_all=ks, round_deaths={1: [(k["tick"], k["victim_steam_id"]) for k in ks]})
    multi = [m for m in ms if m["type"] == "multikill"]
    assert len(multi) == 1
    m = multi[0]
    assert m["kill_count"] == 3
    assert m["pov_candidates"][0]["steam_id"] == PRO
    assert m["primary_pro_sid"] == PRO
    assert m["tier_ok"] is True


def test_clutch_won_marks_clutcher():
    # Everyone on PRO's team dies but PRO: 1v5, PRO gets 2, team wins.
    mates = [s for s in T2 if s != PRO]
    ks = ([_kill(2000 + n * 100, 1, E1, m) for n, m in enumerate(mates)]
          + [_kill(3000, 1, PRO, E1), _kill(3200, 1, PRO, E2)])
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _run(kills_all=ks, round_deaths=deaths, team_by_sid=dict(TEAMS_5V5))
    cl = [m for m in ms if m["type"] == "clutch"]
    assert len(cl) == 1
    # Trigger fires at the first 2v5-or-worse state (fourth mate still alive).
    assert cl[0]["clutch_initial_count"] == "2v5"
    assert cl[0]["won"] is True
    assert cl[0]["pov_candidates"][0] == {
        "steam_id": PRO, "role": "clutcher", "is_pro": True}
    assert cl[0]["primary_pro_sid"] == PRO


def test_clutch_attempt_when_lost():
    mates = [s for s in T2 if s != PRO]
    ks = ([_kill(2000 + n * 100, 1, E1, m) for n, m in enumerate(mates)]
          + [_kill(3000, 1, PRO, E1),   # 1v5 trigger team gets one...
             _kill(3100, 1, E3, PRO)])  # ...then wiped
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _run(kills_all=ks, round_deaths=deaths, team_by_sid=dict(TEAMS_5V5),
              winner_by_round={1: 3})
    assert [m["type"] for m in ms if "clutch" in m["type"]] == ["clutch_attempt"]
    att = next(m for m in ms if m["type"] == "clutch_attempt")
    assert att["won"] is False
    assert att["primary_pro_sid"] == PRO


def test_ct_clock_win_is_not_a_clutch():
    mates = [s for s in T2 if s != PRO]
    ks = ([_kill(2000 + n * 100, 1, E1, m) for n, m in enumerate(mates)]
          + [_kill(3000, 1, PRO, E1), _kill(3200, 1, PRO, E2)])
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _run(kills_all=ks, round_deaths=deaths, team_by_sid=dict(TEAMS_5V5),
              win_reason_by_round={1: "time_ran_out"})
    assert not [m for m in ms if "clutch" in m["type"]]


def test_opener_and_trade():
    ks = [
        _kill(2000, 1, PRO, E1),     # opener
        _kill(2100, 1, E2, MATE),    # E2 kills PRO's mate...
        _kill(2250, 1, PRO, E2),     # ...PRO trades inside the window
    ]
    deaths = {1: [(2000, E1), (2100, MATE), (2250, E2)]}
    ms = _run(kills_all=ks, round_deaths=deaths)
    kinds = {m["type"] for m in ms}
    assert "opener" in kinds and "trade" in kinds
    op = next(m for m in ms if m["type"] == "opener")
    assert op["attacker_steam_id"] == PRO
    tr = next(m for m in ms if m["type"] == "trade")
    assert tr["attacker_steam_id"] == PRO


def test_bomb_plant_moment_names_planter():
    b = [{"tick": 5000, "round": 1, "type": "plant",
          "player": "x", "player_steam_id": MATE, "site": "A"}]
    ms = _run(bomb_actions=b)
    plants = [m for m in ms if m["type"] == "bomb_plant"]
    assert len(plants) == 1
    assert plants[0]["site"] == "A"
    assert plants[0]["primary_pro_sid"] == MATE


def test_rando_only_moment_has_no_pro_pov():
    ks = [_kill(2000, 1, RANDO, E1)]
    deaths = {1: [(2000, E1)]}
    ms = _run(kills_all=ks, round_deaths=deaths,
              team_by_sid={RANDO: 2, E1: 3, E2: 3, E3: 3, E4: 3, MATE: 3},
              pro_sids={})
    op = next(m for m in ms if m["type"] == "opener")
    assert op["primary_pro_sid"] == ""
    assert op["pov_candidates"][0]["steam_id"] == RANDO


def test_pov_candidates_pros_first_capped():
    cands, primary = _pov_candidates(
        [RANDO, PRO, E1, MATE, E2], {}, PRO_SIDS)
    assert [c["steam_id"] for c in cands] == [PRO, MATE, RANDO, E1]
    assert primary == PRO


def _throw(tick, rnd, sid, util):
    return {"tick": tick, "round": rnd, "player": sid,
            "player_steam_id": sid, "util": util, "x": None, "y": None}


def test_util_kill_moment_for_he():
    ks = [_kill(2000, 1, PRO, E1, weapon="hegrenade")]
    deaths = {1: [(2000, E1)]}
    # mark it a util kill the way the builder does
    ks[0]["util_kill"] = True
    ms = _run(kills_all=ks, round_deaths=deaths)
    uk = [m for m in ms if m["type"] == "util_kill"]
    assert len(uk) == 1
    assert uk[0]["primary_pro_sid"] == PRO
    assert uk[0]["label"] == "HE KILL"


def test_util_burst_one_per_team_with_quality_bar():
    throws = [
        _throw(2000, 1, PRO, "smoke"),
        _throw(2050, 1, MATE, "flash"),
        _throw(2100, 1, RANDO, "he"),      # team 2 exec: smoke+flash+he
        _throw(2200, 1, E1, "flash"),
        _throw(2250, 1, E2, "flash"),      # team 3: flashes only -> dropped
    ]
    ms = _run(util_throws=throws)
    bursts = [m for m in ms if m["type"] == "util_burst"]
    assert len(bursts) == 1
    b = bursts[0]
    assert b["throw_count"] == 3
    assert b["primary_pro_sid"] == PRO
    assert b["pov_candidates"][0]["role"] == "exec"


def test_multikill_counts_blinded_kills():
    ks = [_kill(2000 + n * 100, 1, PRO, v)
          for n, v in enumerate([E1, E2, E3])]
    ks[0]["blinded_by"] = MATE
    ms = _run(kills_all=ks, round_deaths={1: [(k["tick"], k["victim_steam_id"]) for k in ks]})
    multi = next(m for m in ms if m["type"] == "multikill")
    assert multi["blinded_kills"] == 1


def test_duel_won_1v1_names_duelists():
    mates = [s for s in T2 if s != PRO]
    foes = [s for s in T3 if s != E1]
    ks = ([_kill(2000 + n * 100, 1, E1, m) for n, m in enumerate(mates)]
          + [_kill(2400 + n * 100, 1, PRO, f) for n, f in enumerate(foes)]
          + [_kill(3000, 1, PRO, E1)])
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _run(kills_all=ks, round_deaths=deaths, team_by_sid=dict(TEAMS_5V5))
    duels = [m for m in ms if m["type"] == "duel"]
    assert len(duels) == 1
    assert duels[0]["duel_state"] == "1v1"
    assert duels[0]["primary_pro_sid"] == PRO


def test_closer_is_last_kill():
    ks = [_kill(2000, 1, PRO, E1), _kill(2500, 1, MATE, E2)]
    deaths = {1: [(2000, E1), (2500, E2)]}
    ms = _run(kills_all=ks, round_deaths=deaths)
    cl = [m for m in ms if m["type"] == "closer"]
    assert len(cl) == 1
    assert cl[0]["attacker_steam_id"] == MATE
    assert cl[0]["kill_ticks"] == [2500]


def test_wallbang_moment_for_penetrated_rifle():
    ks = [_kill(2000, 1, PRO, E1, weapon="awp")]
    ks[0]["penetrated"] = 2
    deaths = {1: [(2000, E1)]}
    ms = _run(kills_all=ks, round_deaths=deaths)
    wb = [m for m in ms if m["type"] == "wallbang"]
    assert len(wb) == 1
    assert wb[0]["primary_pro_sid"] == PRO


def _hurt(tick, rnd, vid, hp):
    return {"tick": tick, "round": rnd, "victim_steam_id": vid, "health": hp}


def test_danger_when_pro_survives_low():
    import highlights.build_action_timeline as bat
    ks = [_kill(2000, 1, E1, MATE)]
    deaths = {1: [(2000, MATE)]}
    hurts = [_hurt(1900, 1, PRO, 17.0)]
    ms = bat._derive_moments(
        ks, deaths, {1: 1000}, {1: 9000}, {1: 3}, {1: "t_killed"}, [],
        dict(TEAMS_5V5), dict(PRO_SIDS), util_throws=[],
        all_hurts=hurts)
    dg = [m for m in ms if m["type"] == "danger"]
    assert len(dg) == 1
    assert dg[0]["primary_pro_sid"] == PRO
    assert dg[0]["pov_candidates"][0]["role"] == "survivor"


def test_no_danger_when_pro_dies_immediately():
    ks = [_kill(2000, 1, E1, MATE), _kill(2050, 1, E1, PRO)]
    deaths = {1: [(2000, MATE), (2050, PRO)]}
    hurts = [_hurt(2050, 1, PRO, 0.0)]
    import highlights.build_action_timeline as bat
    ms = bat._derive_moments(
        ks, deaths, {1: 1000}, {1: 9000}, {1: 3}, {1: "t_killed"}, [],
        dict(TEAMS_5V5), dict(PRO_SIDS), util_throws=[], all_hurts=hurts)
    assert not [m for m in ms if m["type"] == "danger"]
