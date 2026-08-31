from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

import daily_notable as dn
import scrape_notable as sn
import update_player_demand as upd


@pytest.fixture(autouse=True)
def _frozen_demand_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "DEMAND_INDEX_PATH", tmp_path / "missing.json")


def test_market_demand_bonus_uses_research_index():
    assert sn.market_demand_bonus("ropz") == 172_500
    assert sn.market_demand_bonus("DONK") == 125_000
    assert sn.market_demand_bonus("dev1ce") == 70_000
    assert sn.market_demand_bonus("rain") == 30_000
    assert sn.market_demand_bonus("TeSeS") == 87_500
    assert sn.market_demand_bonus("nocries") == 57_500
    assert sn.market_demand_bonus("unmeasured") == 0


def test_market_demand_bonus_reads_live_index_file(tmp_path, monkeypatch):
    path = tmp_path / "player_demand_index.json"
    path.write_text('{"index": {"donk": 1.8}}', encoding="utf-8")
    monkeypatch.setattr(sn, "DEMAND_INDEX_PATH", path)
    assert sn.market_demand_bonus("DONK") == 200_000
    assert sn.market_demand_bonus("ropz") == 0


def test_build_index_blends_recent_lift_and_ignores_unknown_names():
    long_report = {
        "groups": {
            "primary_players": [
                {"label": "donk", "videos": 20, "median_performance_index": 1.2},
                {"label": "ropz", "videos": 40, "median_performance_index": 1.69},
                {"label": "Smoke", "videos": 20, "median_performance_index": 5.0},
                {"label": "device", "videos": 8, "median_performance_index": 0.85},
            ]
        }
    }
    recent_report = {
        "groups": {
            "primary_players": [
                {"label": "donk", "videos": 4, "median_performance_index": 1.8},
                {"label": "device", "videos": 3, "median_performance_index": 1.5},
            ]
        }
    }
    aliases = {"donk": "donk", "device": "device", "dev1ce": "device", "ropz": "ropz"}
    index, details = upd.build_index(long_report, recent_report, aliases)
    assert "smoke" not in index
    assert index["donk"] == 1.35
    assert index["ropz"] == 1.69
    assert "device" not in index
    assert details["device"]["index"] == round(
        min(1.35, max(0.85, 0.7 * 0.85 + 0.3 * 1.5)), 2
    )
    assert details["donk"]["index"] == 1.35


def test_lobby_elo_bonus_is_bounded():
    assert sn.lobby_elo_bonus(2400) == 0
    assert sn.lobby_elo_bonus(3250) == 150_000
    assert sn.lobby_elo_bonus(4100) == 300_000


def test_costar_bonus_starts_at_three_pros():
    assert sn.costar_bonus(["donk"]) == 0
    assert sn.costar_bonus(["donk", "magixx"]) == 0
    assert sn.costar_bonus(["donk", "magixx", "zont1x"]) == 40_000
    assert sn.costar_bonus(["a", "b", "c", "d", "e"]) == 120_000


def test_performance_win_is_the_watchable_gate():
    assert sn._perf_bonus(2.0, 110, 30, True) == 190_000
    assert sn._perf_bonus(1.0, 70, 20, True) == 80_000
    assert sn._perf_bonus(2.0, 110, 30, False) == 110_000
    assert sn._perf_bonus(0.77, 70, 10, True) == 0
    assert sn._perf_bonus(5.0, 200, 60, True) == 255_000


def test_star_bonus_pays_on_plus_kd_even_if_they_lost():
    assert sn.star_bonus(400_000, True) == 200_000
    assert sn.star_bonus(400_000, False, kd=1.5) == 200_000
    assert sn.star_bonus(400_000, True, kd=0.77) == 0
    assert sn.star_bonus(250_000, True, kd=1.13) == 125_000


def test_candidate_weight_keeps_explainable_components(monkeypatch):
    monkeypatch.setattr(sn, "star_bonus_for_pros", lambda pros, ranking: 120_000)
    record = {
        "id": "match-1",
        "pro": "donk",
        "date": datetime(2026, 8, 28),
        "map": "Mirage",
        "score": "13-8",
        "avg_elo": 3500,
        "line": {
            "kd": 2.0,
            "adr": 110,
            "kills": 30,
            "deaths": 15,
            "result": "1",
        },
    }

    candidate = sn.make_player_candidates(record, "solo", {})[0]

    assert candidate["score_version"] == 5
    assert candidate["raw_star_bonus"] == 120_000
    assert candidate["star_bonus"] == 60_000
    assert candidate["market_demand_bonus"] == 125_000
    assert candidate["lobby_elo_bonus"] == 200_000
    assert candidate["costar_bonus"] == 0
    assert candidate["perf_bonus"] == 190_000
    assert candidate["weight"] == 575_000


def test_winning_carry_beats_losing_org_star_in_same_lobby(monkeypatch):
    """Replay of 1-3e07ac85 (apEX 15-14 loss vs HeavyGod 17-15 win).

    v2 picked apEX because Vitality star/4 + demand outran a 10k win chip.
    Breakout competitor POVs are the winning carry, not the stomped star.
    """
    def _stars(pros, ranking):
        return {"apEX": 400_000, "HeavyGod": 250_000}.get(pros[0], 0)

    monkeypatch.setattr(sn, "star_bonus_for_pros", _stars)
    rec = {
        "id": "1-3e07ac85-5fcd-4f75-bc7f-dce7fa9fd0d5",
        "pros": ["HeavyGod", "apEX", "b1t", "jL"],
        "date": datetime(2026, 8, 28),
        "map": "Mirage",
        "score": "13 / 7",
        "avg_elo": 3490,
        "players": {
            "apEX": {
                "kd": 1.07, "adr": 92.7, "kills": 15, "deaths": 14, "result": "0",
            },
            "HeavyGod": {
                "kd": 1.13, "adr": 107.8, "kills": 17, "deaths": 15, "result": "1",
            },
        },
    }
    ranked = sn.make_player_candidates(rec, "multi", {})
    ranked.sort(key=lambda c: -c["weight"])
    assert [c["player"] for c in ranked] == ["HeavyGod", "apEX"]
    assert ranked[0]["star_bonus"] == 125_000
    assert ranked[1]["star_bonus"] == 200_000
    assert ranked[0]["won"] is True
    assert ranked[1]["won"] is False


def test_five_stack_filler_loses_to_solo_breakout(monkeypatch):
    """Recent pool: xeedo 13-14 in a 5-pro queue vs sh1ro 28-9 soloQ."""
    monkeypatch.setattr(sn, "star_bonus_for_pros", lambda pros, ranking: (
        400_000 if pros[0] == "sh1ro" else 0
    ))
    multi = [{
        "id": "stack",
        "pros": ["X5G7V", "clax", "kashl1d", "qw1nk1", "xeedo"],
        "date": datetime(2026, 8, 28),
        "map": "Ancient",
        "score": "9 / 13",
        "avg_elo": 3427,
        "players": {
            "xeedo": {
                "kd": 0.93, "adr": 70, "kills": 13, "deaths": 14, "result": "1",
            },
        },
    }]
    solo = [{
        "id": "breakout",
        "pro": "sh1ro",
        "date": datetime(2026, 8, 28),
        "map": "Cache",
        "score": "13 / 7",
        "avg_elo": 3128,
        "line": {
            "kd": 3.11, "adr": 110, "kills": 28, "deaths": 9, "result": "1",
        },
    }]
    ranked = sn.score_candidates(multi, solo, {})
    assert [c["player"] for c in ranked] == ["sh1ro", "xeedo"]
    assert ranked[0]["weight"] > ranked[1]["weight"]


def test_score_candidates_ranks_by_weight(monkeypatch):
    monkeypatch.setattr(sn, "star_bonus_for_pros", lambda pros, ranking: 0)
    multi = [{
        "id": "match-low",
        "pros": ["magixx"],
        "date": datetime(2026, 8, 28),
        "map": "Dust2",
        "score": "13-11",
        "avg_elo": 2500,
        "players": {
            "magixx": {
                "kd": 1.0, "adr": 70, "kills": 20, "deaths": 20, "result": "0",
            },
        },
    }]
    solo = [{
        "id": "match-high",
        "pro": "ropz",
        "date": datetime(2026, 8, 27),
        "map": "Mirage",
        "score": "13-8",
        "avg_elo": 3500,
        "line": {
            "kd": 2.0, "adr": 110, "kills": 30, "deaths": 15, "result": "1",
        },
    }]

    ranked = sn.score_candidates(multi, solo, {})
    assert [c["player"] for c in ranked] == ["ropz", "magixx"]
    assert ranked[0]["market_demand_bonus"] == 172_500
    assert ranked[1]["market_demand_bonus"] == 0


def test_select_picks_only_one_pov_per_match():
    candidates = [
        {
            "id": "match-1:apEX",
            "match_id": "match-1",
            "player": "apEX",
            "weight": 500,
            "date": "2026-08-28",
        },
        {
            "id": "match-1:b1t",
            "match_id": "match-1",
            "player": "b1t",
            "weight": 490,
            "date": "2026-08-28",
        },
        {
            "id": "match-2:sh1ro",
            "match_id": "match-2",
            "player": "sh1ro",
            "weight": 480,
            "date": "2026-08-28",
        },
    ]

    picks, _, _ = dn.select({"used": []}, candidates, 2, "2026-08-28")

    assert [(pick["match_id"], pick["player"]) for pick in picks] == [
        ("match-1", "apEX"),
        ("match-2", "sh1ro"),
    ]


def test_replay_days_uses_each_day_window_and_does_not_reuse_picks():
    candidates = [
        {"id": "a:donk", "match_id": "a", "player": "donk", "weight": 900,
         "date": "2026-08-25", "kills": 30, "deaths": 10, "won": True},
        {"id": "b:sh1ro", "match_id": "b", "player": "sh1ro", "weight": 800,
         "date": "2026-08-26", "kills": 28, "deaths": 9, "won": True},
        {"id": "c:m0NESY", "match_id": "c", "player": "m0NESY", "weight": 700,
         "date": "2026-08-27", "kills": 22, "deaths": 12, "won": True},
    ]
    by_day = dn.replay_days(candidates, "2026-08-25", "2026-08-27", 1)
    assert [c["player"] for c in by_day["2026-08-25"]] == ["donk"]
    assert [c["player"] for c in by_day["2026-08-26"]] == ["sh1ro"]
    assert [c["player"] for c in by_day["2026-08-27"]] == ["m0NESY"]


def test_day_already_picked_counts_empty_list():
    state = {"last_day": "2026-09-01", "picks": {"2026-09-01": []}}
    assert dn.day_already_picked(state, "2026-09-01")
    assert not dn.day_already_picked(state, "2026-09-02")


def test_card_for_pick_matches_player_and_match(tmp_path):
    card_dir = tmp_path / "backlog" / "faceit" / "2026-09-01" / "high"
    card_dir.mkdir(parents=True)
    (card_dir / "donk-mirage-match-1.json").write_text(json.dumps({
        "player": "donk",
        "faceit_match_id": "match-1",
        "faceit_nickname": "s1mpleDonk",
        "map": "Mirage",
    }), encoding="utf-8")
    (card_dir / "ropz-mirage-match-1.json").write_text(json.dumps({
        "player": "ropz",
        "faceit_match_id": "match-1",
        "map": "Mirage",
    }), encoding="utf-8")
    path = dn.card_for_pick({"match_id": "match-1", "player": "donk"}, root=tmp_path)
    assert path is not None
    assert path.name.startswith("donk-")
    assert dn.rel_card_for_pick(
        {"match_id": "match-1", "player": "donk"}, root=tmp_path
    ) == "backlog/faceit/2026-09-01/high/donk-mirage-match-1.json"
