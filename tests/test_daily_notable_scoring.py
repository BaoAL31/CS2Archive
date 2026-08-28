from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

import daily_notable as dn
import scrape_notable as sn


def test_market_demand_bonus_uses_research_index():
    assert sn.market_demand_bonus("ropz") == 172_500
    assert sn.market_demand_bonus("DONK") == 80_000
    assert sn.market_demand_bonus("unmeasured") == 0


def test_lobby_elo_bonus_is_bounded():
    assert sn.lobby_elo_bonus(2400) == 0
    assert sn.lobby_elo_bonus(3250) == 150_000
    assert sn.lobby_elo_bonus(4100) == 300_000


def test_costar_bonus_starts_at_three_pros():
    assert sn.costar_bonus(["donk"]) == 0
    assert sn.costar_bonus(["donk", "magixx"]) == 0
    assert sn.costar_bonus(["donk", "magixx", "zont1x"]) == 100_000


def test_performance_is_bounded_and_win_is_minor():
    assert sn._perf_bonus(2.0, 110, 30, True) == 120_000
    assert sn._perf_bonus(1.0, 70, 20, True) == 10_000
    assert sn._perf_bonus(5.0, 200, 60, True) == 185_000


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

    assert candidate["score_version"] == 2
    assert candidate["raw_star_bonus"] == 120_000
    assert candidate["star_bonus"] == 30_000
    assert candidate["market_demand_bonus"] == 80_000
    assert candidate["lobby_elo_bonus"] == 200_000
    assert candidate["costar_bonus"] == 0
    assert candidate["perf_bonus"] == 120_000
    assert candidate["weight"] == 430_000


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
