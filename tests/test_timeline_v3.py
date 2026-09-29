"""Timeline v3: stakes, zones, per-pro ledger — pure functions, synthetic data.

No demo needed. Covers buy classification thresholds, _derive_stakes
(streaks/deficit/swing/conversion/arc + unknown-equip fallback), zone
attachment in _derive_moments, _derive_pro_ledger (tags/arcs/revenge), and
multipov acceptance of v3 timelines.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


from cs2archive.highlights.build_action_timeline import (  # noqa: E402
    STAKES_VERSION,
    TIMELINE_VERSION,
    _bomb_action_site,
    _classify_buy,
    _derive_moments,
    _derive_pro_ledger,
    _derive_stakes,
    _economy_by_round,
    _round_starts_from_events,
    buys_veto_fires,
)
from cs2archive.highlights.build_multipov_timeline import (  # noqa: E402
    _tainted_rounds,
    build_multipov_timeline,
    moment_value,
)

PRO = "76561198000000001"
MATE = "76561198000000002"
E1 = "76561198000000003"
E2 = "76561198000000004"
E3 = "76561198000000005"
E4 = "76561198000000006"
GHOST = "76561198000000007"

PRO_SIDS = {PRO: "ProPlayer", MATE: "ProMate", GHOST: "Ghost"}
TEAMS = {PRO: 2, MATE: 2, E1: 3, E2: 3, E3: 3, E4: 3, GHOST: 2}


def test_classify_buy_thresholds():
    assert _classify_buy(650, True) == "pistol"
    assert _classify_buy(200, False) == "eco"      # absolute
    # ratio branch dropped (Q1 verdict): a 2000-avg team facing 6x money is
    # an (outgunned) force, never eco.
    assert _classify_buy(2000, False) == "force"
    assert _classify_buy(4400, False) == "full"
    assert _classify_buy(None, False) == "unknown"
    # eco-vs-eco stays eco on both sides
    assert _classify_buy(200, False) == "eco"


def test_version_pins():
    assert TIMELINE_VERSION == 3
    assert STAKES_VERSION == 2


def _rounds5():
    return [
        {"round": 1, "winner_team": 2,
         "score_after": {"2": 1, "3": 0}, "match_point_for": None},
        {"round": 2, "winner_team": 3,
         "score_after": {"2": 1, "3": 1}, "match_point_for": None},
        {"round": 3, "winner_team": 3,
         "score_after": {"2": 1, "3": 2}, "match_point_for": None},
        {"round": 4, "winner_team": 3,
         "score_after": {"2": 1, "3": 3}, "match_point_for": None},
        {"round": 5, "winner_team": 2,
         "score_after": {"2": 2, "3": 3}, "match_point_for": None},
    ]


def _equip5():
    return {
        1: {2: {"total": 3250, "players": 5}, 3: {"total": 3250, "players": 5}},
        2: {2: {"total": 1000, "players": 5}, 3: {"total": 10650, "players": 5}},
        3: {2: {"total": 21000, "players": 5}, 3: {"total": 22000, "players": 5}},
        4: {2: {"total": 20000, "players": 5}, 3: {"total": 21000, "players": 5}},
        5: {2: {"total": 19000, "players": 5}, 3: {"total": 21500, "players": 5}},
    }


def test_derive_stakes_buys_and_streaks():
    stakes, arc = _derive_stakes(_rounds5(), _equip5(), [2, 3])
    assert stakes[1]["buys"] == {"2": "pistol", "3": "pistol"}
    assert stakes[1]["is_pistol"] is True
    assert stakes[2]["buys"] == {"2": "eco", "3": "force"}
    assert stakes[3]["buys"] == {"2": "full", "3": "full"}
    assert stakes[2]["win_streak_before"] == {"2": 1, "3": 0}
    assert stakes[2]["deficit_at_start"] == {"2": 1, "3": -1}
    assert stakes[5]["deficit_at_start"] == {"2": -2, "3": 2}
    # round 5: team 2 ends a 3-round losing run -> swing
    assert stakes[5]["swing"] is True
    assert stakes[4]["swing"] is False
    assert stakes[1]["equip_value"] == {"2": 3250, "3": 3250}
    assert stakes[1]["equip_players"] == {"2": 5, "3": 5}
    assert stakes[1]["buy_source"] == "ticks"


def test_derive_stakes_arc_and_conversion():
    rounds = _rounds5()
    rounds[4] = {**rounds[4], "match_point_for": None}
    rounds[3] = {**rounds[3], "match_point_for": 3}
    stakes, arc = _derive_stakes(rounds, _equip5(), [2, 3])
    assert stakes[4]["conversion"] is True
    assert stakes[5]["conversion"] is False
    assert arc["total_rounds"] == 5
    assert arc["pistol_rounds"] == [1]
    assert arc["lead_changes"] == 1
    assert arc["first_lead_change_round"] == 3
    assert arc["longest_run"] == {"team": 3, "count": 3}
    assert arc["max_deficit"] == {"round": 5, "team": 2, "deficit": 2}


def test_derive_stakes_unknown_equip_still_scores_arc():
    stakes, arc = _derive_stakes(_rounds5(), {}, [2, 3])
    assert stakes[3]["buys"] == {"2": "unknown", "3": "unknown"}
    assert stakes[3]["buy_source"] == "unknown"
    # streaks/deficit need no ticks
    assert stakes[5]["swing"] is True
    assert arc["longest_run"] == {"team": 3, "count": 3}


def test_derive_stakes_partial_team_missing():
    equip = {1: {2: {"total": 3250, "players": 5}}}  # team 3 unsampled
    stakes, _ = _derive_stakes(_rounds5()[:1], equip, [2, 3])
    # round 1 is a pistol round: pistols are game-guaranteed, so even the
    # unsampled team reads pistol rather than unknown.
    assert stakes[1]["buys"] == {"2": "pistol", "3": "pistol"}
    assert stakes[1]["equip_value"] == {"2": 3250, "3": None}
    assert stakes[1]["equip_players"] == {"2": 5, "3": None}
    assert stakes[1]["buy_source"] == "ticks"


def test_derive_stakes_undersampled_team_reads_unknown():
    equip = {2: {2: {"total": 20000, "players": 5},
                 3: {"total": 9000, "players": 2}}}  # 2 of 5 sampled
    stakes, _ = _derive_stakes(_rounds5()[1:2], equip, [2, 3])
    assert stakes[2]["buys"] == {"2": "full", "3": "unknown"}
    assert stakes[2]["equip_players"] == {"2": 5, "3": 2}


def test_derive_stakes_swing_boundary_two_streak_is_not_swing():
    rounds = [
        {"round": 1, "winner_team": 3,
         "score_after": {"2": 0, "3": 1}, "match_point_for": None},
        {"round": 2, "winner_team": 3,
         "score_after": {"2": 0, "3": 2}, "match_point_for": None},
        {"round": 3, "winner_team": 2,
         "score_after": {"2": 1, "3": 2}, "match_point_for": None},
    ]
    stakes, _ = _derive_stakes(rounds, {}, [2, 3])
    assert stakes[3]["swing"] is False  # broken run was only 2, needs >= 3


def _mkill(tick, rnd, aid, vid, place=""):
    return {
        "tick": tick, "round": rnd,
        "attacker": aid, "attacker_steam_id": aid,
        "victim": vid, "victim_steam_id": vid,
        "weapon": "ak47", "victim_weapon": "ak47",
        "is_bomb": False, "headshot": False,
        "attacker_is_pro": aid in PRO_SIDS,
        "victim_is_pro": vid in PRO_SIDS,
        "attacker_place": place, "victim_place": "",
    }


def _base_moments(**over):
    d = dict(
        kills_all=[],
        round_deaths={},
        round_starts={1: 1000},
        round_ends={1: 9000},
        winner_by_round={1: 2},
        win_reason_by_round={1: "t_killed"},
        bomb_actions=[],
        team_by_sid=dict(TEAMS),
        pro_sids={PRO: "ProPlayer", MATE: "ProMate"},
    )
    d.update(over)
    return d


def test_zone_attached_from_attacker_place():
    ks = [_mkill(2000, 1, PRO, E1, place="Banana"),
          _mkill(2500, 1, MATE, E2, place="")]
    deaths = {1: [(2000, E1), (2500, E2)]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths))
    op = next(m for m in ms if m["type"] == "opener")
    assert op["zone"] == {"place": "Banana", "site": ""}
    cl = next(m for m in ms if m["type"] == "closer")
    assert cl["zone"] is None  # last kill has no known place


def test_zone_site_only_for_bomb_moments():
    b = [{"tick": 5000, "round": 1, "type": "plant",
          "player": "x", "player_steam_id": MATE, "site": "BombsiteA"}]
    ms = _derive_moments(**_base_moments(bomb_actions=b))
    plant = next(m for m in ms if m["type"] == "bomb_plant")
    assert plant["zone"] == {"place": "", "site": "BombsiteA"}


def test_bomb_action_site_shapes():
    # plant/defuse read the actor's callout; explode reuses the plant site.
    assert _bomb_action_site("plant", 1, 5000, MATE,
                             lambda t, s: "BombsiteB", {}) == "BombsiteB"
    assert _bomb_action_site("defuse", 1, 5000, MATE,
                             lambda t, s: "BombsiteA", {}) == "BombsiteA"
    assert _bomb_action_site("explode", 1, 9000, MATE,
                             lambda t, s: "Middle",
                             {1: "BombsiteB"}) == "BombsiteB"
    assert _bomb_action_site("explode", 2, 9000, MATE,
                             lambda t, s: "Middle", {}) == "Middle"
    assert _bomb_action_site("plant", 1, 5000, MATE,
                             lambda t, s: "", {}) == ""


def _ledger_fixture():
    ks = [
        _mkill(2000, 1, PRO, E1),   # opener r1 + multi start
        _mkill(2100, 1, PRO, E2),
        _mkill(2200, 1, PRO, E3),   # 3K r1
        _mkill(2300, 1, E4, PRO),   # PRO dies r1
        _mkill(2400, 1, E1, MATE),  # MATE dies r1
        _mkill(3000, 2, PRO, E4),   # revenge r2 (E4 killed PRO r1)
        _mkill(3100, 2, MATE, E1),  # opener r2
    ]
    deaths = {1: [(2000, E1), (2100, E2), (2200, E3), (2300, PRO), (2400, MATE)],
              2: [(3000, E4), (3100, E1)]}
    ms = _derive_moments(**_base_moments(
        kills_all=ks, round_deaths=deaths,
        round_starts={1: 1000, 2: 10000}, round_ends={1: 9000, 2: 19000},
        winner_by_round={1: 3, 2: 2}))
    return ks, deaths, ms


def test_pro_ledger_totals_tags_and_arc():
    ks, deaths, ms = _ledger_fixture()
    pros = _derive_pro_ledger(ks, deaths, ms, [1, 2], PRO_SIDS, TEAMS,
                              {1: 2, 2: 2})
    by_sid = {p["steam_id"]: p for p in pros}
    assert set(by_sid) == {PRO, MATE}  # GHOST uninvolved -> omitted
    pro = by_sid[PRO]
    assert pro["nick"] == "ProPlayer"
    assert pro["totals"]["kills"] == 4
    assert pro["totals"]["deaths"] == 1
    r1 = next(r for r in pro["rounds"] if r["round"] == 1)
    assert r1["kills"] == 3 and r1["openers"] == 1 and r1["openers_won"] == 1
    assert r1["multikills"] == 1
    assert set(r1["tags"]) == {"entry", "multi"}
    assert pro["arc"]["best_round"] == 1
    assert pro["arc"]["silent_rounds"] == []
    assert pro["arc"]["longest_streak"] == 2
    assert pro["arc"]["revenge"] == [{"round": 2, "on_steam_id": E4}]
    mate = by_sid[MATE]
    assert mate["totals"] == {"kills": 1, "deaths": 1,
                              "multikills": 0, "clutches_won": 0}
    assert next(r for r in mate["rounds"]
                if r["round"] == 1)["tags"] == ["silent"]


def test_pro_ledger_lost_opener_is_factual_not_won():
    ks = [_mkill(2000, 1, PRO, E1)]
    deaths = {1: [(2000, E1)]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths,
                                         winner_by_round={1: 3}))
    pros = _derive_pro_ledger(ks, deaths, ms, [1], PRO_SIDS, TEAMS, {1: 3})
    pro = next(p for p in pros if p["steam_id"] == PRO)
    r1 = pro["rounds"][0]
    assert (r1["openers"], r1["openers_won"]) == (1, 0)
    # entry stays factual (opener role, won or lost) — flagged for council.
    assert "entry" in r1["tags"]


def test_pro_ledger_counts_teamkill_deaths():
    ks = [_mkill(2000, 1, PRO, E1)]
    # PRO dies with no killer in kills_all (teamkill/suicide/world).
    deaths = {1: [(2000, E1), (2050, PRO)]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths))
    pros = _derive_pro_ledger(ks, deaths, ms, [1], PRO_SIDS, TEAMS, {1: 2})
    pro = next(p for p in pros if p["steam_id"] == PRO)
    assert pro["totals"]["deaths"] == 1


def test_round_starts_keeps_hltv_round_one():
    # HLTV emits round 1 at tick 1; tick-0 rows without a round are phantoms.
    assert _round_starts_from_events([(1, 1), (5000, 2)]) == {1: 1, 2: 5000}
    assert _round_starts_from_events([(0, 0), (1, 1), (5000, 2)]) == {1: 1, 2: 5000}
    assert _round_starts_from_events([]) == {}


def test_round_starts_duplicates_last_wins():
    # FACEIT repeats round starts; the latest tick wins (unchanged behavior).
    assert _round_starts_from_events(
        [(0, 1), (2939, 1), (3186, 1), (9000, 2)]) == {1: 3186, 2: 9000}


def test_round_starts_sequential_fallback_without_round_numbers():
    assert _round_starts_from_events([(100, 0), (9000, 0)]) == {1: 100, 2: 9000}


def test_pro_ledger_best_round_none_without_kills():
    ks = [_mkill(2000, 1, E1, MATE)]
    deaths = {1: [(2000, MATE)]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths))
    pros = _derive_pro_ledger(ks, deaths, ms, [1], PRO_SIDS)
    mate = next(p for p in pros if p["steam_id"] == MATE)
    assert mate["totals"]["kills"] == 0
    assert mate["arc"]["best_round"] is None
    assert mate["arc"]["silent_rounds"] == [1]


def test_multipov_accepts_v3_timeline():
    at = {
        "timeline_version": 3,
        "stakes_version": 1,
        "rounds": [{"round": 1, "start_tick": 0, "end_tick": 1000,
                    "hot_zones": ["Banana"],
                    "stakes": {"buys": {"2": "pistol", "3": "pistol"}}}],
        "moments": [
            {"id": "r1-multikill-0", "type": "multikill", "round": 1,
             "start_tick": 0, "end_tick": 640, "kill_ticks": [],
             "primary_pro_sid": PRO, "label": "3K", "kill_count": 3,
             "tier_ok": True,
             "zone": {"place": "Banana", "site": ""}},
        ],
        "pros": [{"steam_id": PRO, "nick": "ProPlayer"}],
    }
    edit = build_multipov_timeline(at, max_minutes=5)
    assert edit["kind"] == "multipov"
    assert edit["stats"]["picked_count"] == 1
    assert edit["picked"][0]["zone"] == {"place": "Banana", "site": ""}


def _veto_rounds():
    return {1: {"stakes": {"buy_source": "ticks",
                           "buys": {"2": "full", "3": "eco"}}},
            2: {"stakes": {"buy_source": "ticks",
                           "buys": {"2": "eco", "3": "full"}}},
            3: {"stakes": {"buy_source": "unknown",
                           "buys": {"2": "unknown", "3": "unknown"}}}}


def _vmom(rnd, **kw):
    d = {"id": f"r{rnd}-multikill-1", "type": "multikill", "round": rnd,
         "start_tick": 0, "end_tick": 640, "kill_ticks": [],
         "primary_pro_sid": PRO, "label": "4K", "kill_count": 4,
         "tier_ok": True, "attacker_steam_id": PRO}
    d.update(kw)
    return d


def test_buys_veto_fires_shapes():
    assert buys_veto_fires({"2": "full", "3": "eco"}, [3], 4) is True
    assert buys_veto_fires({"2": "full", "3": "eco"}, [3], 3) is False
    assert buys_veto_fires({"2": "eco", "3": "eco"}, [2, 3], 5) is True
    assert buys_veto_fires({"2": "full", "3": "force"}, [3], 4) is False
    assert buys_veto_fires({"2": "full", "3": "unknown"}, [3], 4) is False
    assert buys_veto_fires({"2": "full"}, [3], 4) is False
    assert buys_veto_fires({"2": "full", "3": "eco"}, [], 4) is False
    assert buys_veto_fires({}, [3], 4) is False


def test_moment_value_buys_veto_zeroes_farmed_4k():
    m = _vmom(1, victim_teams=[3])
    v, why = moment_value(m, set(), _veto_rounds())
    assert v == 0.0
    assert any("all-eco" in w for w in why)


def test_moment_value_eco_hero_promotion():
    m = _vmom(2, victim_teams=[3], attacker_team=2)
    v, why = moment_value(m, set(), _veto_rounds())
    assert v == 40.0 * 1.5
    assert any("eco hero" in w for w in why)


def test_moment_value_buys_inert_without_tick_data():
    m = _vmom(3, victim_teams=[3])
    v, _ = moment_value(m, set(), _veto_rounds())  # buy_source unknown
    assert v == 40.0
    v2, _ = moment_value(m, set())  # v2 path, no rounds at all
    assert v2 == 40.0
    # 3K vs eco is not vetoed (veto needs 4K+)
    m3 = _vmom(1, victim_teams=[3], kill_count=3, label="3K")
    v3, _ = moment_value(m3, set(), _veto_rounds())
    assert v3 == 25.0


def test_tainted_rounds_unions_buys_veto():
    moms = [_vmom(1, victim_teams=[3]), _vmom(2, victim_teams=[3])]
    assert _tainted_rounds(moms) == set()  # tier_ok everywhere
    assert _tainted_rounds(moms, _veto_rounds()) == {1}


def test_multipov_v2_parity_and_provenance():
    at = {
        "timeline_version": 2,
        "rounds": [{"round": 1, "start_tick": 0, "end_tick": 1000}],
        "moments": [
            {"id": "r1-multikill-0", "type": "multikill", "round": 1,
             "start_tick": 0, "end_tick": 640, "kill_ticks": [],
             "primary_pro_sid": PRO, "label": "3K", "kill_count": 3,
             "tier_ok": True},
        ],
    }
    edit = build_multipov_timeline(at, max_minutes=5)
    assert edit["timeline_version"] == 3
    assert edit["source_timeline_version"] == 2
    assert edit["stakes_version"] is None
    assert edit["stats"]["picked_count"] == 1
    assert edit["picked"][0]["value"] == 25.0
    assert edit["picked"][0]["why"] == ["multikill:3K=25"]


def test_zone_first_kill_wins_on_multikill():
    ks = [_mkill(2000, 1, PRO, E1, place="Middle"),
          _mkill(2100, 1, PRO, E2, place="BombsiteB"),
          _mkill(2200, 1, PRO, E3, place="BombsiteB")]
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths))
    multi = next(m for m in ms if m["type"] == "multikill")
    assert multi["zone"] == {"place": "Middle", "site": ""}


def test_zone_walks_past_non_kill_clutch_tick():
    t2 = [PRO, MATE, "76561198000000008", "76561198000000009", GHOST]
    t3 = [E1, E2, E3, E4, "76561198000000010"]
    teams = {s: 2 for s in t2} | {s: 3 for s in t3}
    mates = [s for s in t2 if s != PRO]
    ks = ([_mkill(2000 + n * 100, 1, E1, m) for n, m in enumerate(mates)]
          + [_mkill(3000, 1, PRO, E1, place="Banana"),
             _mkill(3200, 1, PRO, E2, place="Banana")])
    deaths = {1: [(k["tick"], k["victim_steam_id"]) for k in ks]}
    ms = _derive_moments(**_base_moments(kills_all=ks, round_deaths=deaths,
                                         team_by_sid=teams,
                                         winner_by_round={1: 2}))
    cl = [m for m in ms if m["type"] == "clutch"]
    assert len(cl) == 1
    assert cl[0]["kill_ticks"][-1] == 9000  # win tick appended, unmapped
    assert cl[0]["zone"] == {"place": "Banana", "site": ""}


class _StubParser:
    def __init__(self, frame):
        self._frame = frame

    def parse_ticks(self, props, ticks):
        return self._frame


def test_economy_by_round_resolved_and_max():
    import pandas as pd
    frame = pd.DataFrame([
        {"tick": 1000, "steamid": "s1", "current_equip_value": 1000.0},
        {"tick": 1064, "steamid": "s1", "current_equip_value": 5000.0},
        {"tick": 1000, "steamid": "s2", "current_equip_value": 200.0},
        {"tick": 1064, "steamid": "s2", "current_equip_value": 300.0},
    ])
    out, resolved = _economy_by_round(_StubParser(frame), {1: 1000})
    assert resolved is True
    assert out[1]["s1"] == 5000.0  # max over freeze and +64
    assert out[1]["s2"] == 300.0


def test_economy_by_round_unresolved_paths():
    import pandas as pd
    # prop silently dropped -> unresolved, never zeros
    frame = pd.DataFrame([{"tick": 1000, "steamid": "s1"}])
    out, resolved = _economy_by_round(_StubParser(frame), {1: 1000})
    assert (out, resolved) == ({}, False)
    # empty frame -> unresolved
    out, resolved = _economy_by_round(
        _StubParser(pd.DataFrame(columns=["tick"])), {1: 1000})
    assert (out, resolved) == ({}, False)
    # no freeze rounds -> unresolved
    out, resolved = _economy_by_round(_StubParser(frame), {})
    assert (out, resolved) == ({}, False)
