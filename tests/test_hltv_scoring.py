from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from hltv import match_listener as listener
from hltv.score_cards import (
    match_highlight_bonus,
    parse_kd_ratio,
    rating_bonus,
    score_card,
)
from hltv.update_team_demand import (
    build_index,
    canonical_team,
    extract_fixture_teams,
    resolve_team_name,
    team_lookup,
)


LOOKUP = team_lookup([
    "Spirit", "FURIA", "Vitality", "Legacy", "G2", "Aurora",
    "Natus Vincere", "FaZe", "9z", "paiN", "The MongolZ",
])


def test_extract_fixture_teams_common_highlight_titles():
    assert extract_fixture_teams(
        "Vitality vs Legacy - BLAST Open Porto 2026", LOOKUP
    ) == ("Vitality", "Legacy")
    assert extract_fixture_teams(
        "G2 vs Aurora | BLAST Open Porto | Quarter-Final", LOOKUP
    ) == ("G2", "Aurora")
    assert extract_fixture_teams(
        "IEM Cologne 2026 | Spirit vs FURIA | Highlights", LOOKUP
    ) == ("Spirit", "FURIA")
    assert extract_fixture_teams("NaVi vs FaZe - BLAST", LOOKUP) == (
        "Natus Vincere", "FaZe",
    )
    assert extract_fixture_teams("The MongolZ vs 9z", LOOKUP) == (
        "The MongolZ", "9z",
    )


def test_canonical_team_aliases():
    assert canonical_team("navi", LOOKUP) == "Natus Vincere"
    assert canonical_team("Furia", LOOKUP) == "FURIA"
    assert canonical_team("mongolz", LOOKUP) == "The MongolZ"
    assert canonical_team("not-a-team", LOOKUP) is None
    assert resolve_team_name("furia blast open porto", LOOKUP) == "FURIA"
    assert resolve_team_name("spirit", LOOKUP) == "Spirit"


def test_parse_kd_handles_hltv_dashes():
    assert parse_kd_ratio("20-10") == 2.0
    assert parse_kd_ratio("20–10") == 2.0
    assert parse_kd_ratio("15-0") == 15.0
    assert parse_kd_ratio("") == 0.0
    assert parse_kd_ratio(1.4) == 1.4


def test_rating_bonus_scale():
    assert rating_bonus(1.0) == 0
    assert rating_bonus(1.5) == 40_000
    assert rating_bonus(2.0) == 80_000
    assert rating_bonus(3.5) == 160_000


def test_build_index_attributes_highlights_to_both_teams():
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    samples = [
        ("Spirit vs FURIA - BLAST", 400_000, 40_000),
        ("Spirit vs Vitality | Highlights", 300_000, 30_000),
        ("Spirit vs G2 | BLAST", 280_000, 28_000),
        ("donk ACE | Spirit vs FURIA", 500_000, 50_000),
        ("donk 1v3 | Spirit vs Legacy", 220_000, 22_000),
        ("FURIA vs Legacy - BLAST", 80_000, 8_000),
        ("FURIA vs G2 - BLAST", 90_000, 9_000),
        ("FURIA vs Aurora - BLAST", 70_000, 7_000),
        ("Vitality vs Legacy - BLAST", 120_000, 12_000),
        ("G2 vs Aurora | BLAST", 50_000, 5_000),
        ("G2 vs Vitality - BLAST", 90_000, 9_000),
        ("G2 vs Legacy - BLAST", 40_000, 4_000),
        ("G2 vs 9z - BLAST", 35_000, 3_500),
        ("9z vs paiN | BLAST", 10_000, 1_000),
        ("donk vs FURIA | BLAST Open Porto", 180_000, 18_000),
    ]
    rows = []
    for i, (title, views, vpd) in enumerate(samples):
        rows.append({
            "title": title,
            "views": views,
            "views_per_day": vpd,
            "age_days": 10,
            "duration_seconds": 600,
            "channel": "BLAST CS2 Highlights",
            "video_id": f"vid{i}",
            "published_at": "2026-08-10T00:00:00+00:00",
        })
    payload = build_index(rows, aliases={"donk": "donk"}, lookup=LOOKUP, now=now)
    assert payload["index"]["Spirit"] == max(payload["index"].values())
    assert payload["players"]["donk"] >= 1.08
    pairs = {(f["team1"], f["team2"]) for f in payload["fixtures"]}
    assert ("Spirit", "FURIA") in pairs


def test_match_highlight_bonus_log_scale():
    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    fixtures = [{
        "team1": "Spirit",
        "team2": "FURIA",
        "views": 1_000_000,
        "published_at": (now - timedelta(hours=6)).isoformat(),
    }]
    assert match_highlight_bonus("Spirit", "FURIA", fixtures, now=now) == 200_000
    fixtures[0]["views"] = 10_000
    assert match_highlight_bonus("Spirit", "FURIA", fixtures, now=now) == round(
        ((math.log10(10_000) - 3.0) / 3.0) * 200_000
    )
    fixtures[0]["published_at"] = (now - timedelta(days=20)).isoformat()
    assert match_highlight_bonus("Spirit", "FURIA", fixtures, now=now) == 0


def test_score_card_chip_math():
    scored = score_card(
        {
            "player": "donk",
            "team": "Spirit",
            "opponent": "FURIA",
            "rating": 1.3,
            "kd": "18-12",
            "map": "Mirage",
        },
        ranking={"Spirit": 1, "FURIA": 3},
        player_demand={"donk": 1.53},
        team_demand={"Spirit": 1.5, "FURIA": 1.4},
        highlight_players={},
        fixtures=[],
    )
    assert scored["star_bonus"] == 200_000
    assert scored["market_demand_bonus"] == 132_500
    assert scored["match_team_bonus"] == 125_000 + 100_000
    assert scored["rating_bonus"] == 24_000
    assert scored["match_highlight_bonus"] == 0
    assert scored["weight"] == 581_500


def test_score_card_resolves_event_suffix_from_fixture_slug():
    scored = score_card(
        {
            "player": "donk",
            "team": "Spirit",
            "opponent": "",
            "rating": 1.3,
            "kd": "18-12",
        },
        ranking={"Spirit": 1, "FURIA": 3},
        player_demand={"donk": 1.53},
        team_demand={"Spirit": 1.5, "FURIA": 1.4},
        fixture_teams=("spirit", "furia blast open porto"),
    )
    assert scored["match_team_bonus"] == 125_000 + 100_000


def test_star_player_medium_beats_unknown_high():
    donk = score_card(
        {
            "player": "donk",
            "team": "Spirit",
            "opponent": "FURIA",
            "rating": 1.3,
            "kd": "18-12",
        },
        ranking={"Spirit": 1, "FURIA": 3, "9z": 12},
        player_demand={"donk": 1.53},
        team_demand={"Spirit": 1.5, "FURIA": 1.4, "9z": 1.1},
    )
    nobody = score_card(
        {
            "player": "unknown",
            "team": "9z",
            "opponent": "paiN",
            "rating": 1.8,
            "kd": "22-10",
        },
        ranking={"Spirit": 1, "FURIA": 3, "9z": 12},
        player_demand={"donk": 1.53},
        team_demand={"Spirit": 1.5, "FURIA": 1.4, "9z": 1.1},
    )
    assert donk["weight"] > nobody["weight"]


def test_minus_kd_drops_org_star():
    scored = score_card(
        {"player": "karrigan", "team": "Falcons", "rating": 1.6, "kd": "10-13"},
        ranking={"Falcons": 2},
        player_demand={},
        team_demand={},
    )
    assert scored["star_bonus"] == 0
    assert scored["rating_bonus"] == 48_000


def test_select_prefers_weight_over_rating():
    cards = [
        ("backlog/nobody.json", {
            "player": "x", "map": "Mirage", "rating": 1.9, "weight": 40_000,
        }),
        ("backlog/donk.json", {
            "player": "donk", "map": "Mirage", "rating": 1.3, "weight": 300_000,
        }),
        ("backlog/other-map.json", {
            "player": "y", "map": "Nuke", "rating": 2.1, "weight": 50_000,
        }),
    ]
    assert listener.select_best_card(cards)[0][0] == "backlog/donk.json"


def test_queue_sorts_by_weight_then_rating():
    cards = [
        ("backlog/high-rating.json", {"rating": 2.4, "weight": 10_000}),
        ("backlog/star.json", {"rating": 1.3, "weight": 400_000}),
        ("backlog/mid.json", {"rating": 1.8, "weight": 10_000}),
    ]
    assert listener.sort_card_records(cards) == [
        "backlog/star.json",
        "backlog/high-rating.json",
        "backlog/mid.json",
    ]
