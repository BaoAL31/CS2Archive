"""Seams for the dataset-backed POV fitter (LIM pro channel)."""
from __future__ import annotations

from shorts.fit_clip_weights import spearman
from shorts.fit_pov_weights import (
    features_from_row,
    new_weights,
    predict_log_views,
    sgd_epoch,
)


def _row(**over):
    base = {"video_id": "x", "published_at": "2026-06-01T00:00:00+00:00",
            "target_views": 10000, "target_vpd": 1000.0,
            "player": "frozen", "org": "FaZe", "map": "mirage",
            "opp": "PARIVISION", "opp_tier": "top20",
            "rating": 1.2, "rating_bucket": "1.2+",
            "kd": 1.5, "kd_bucket": "1.5+",
            "decider": "no", "won": "yes", "ot": "no",
            "derby_views": 5000, "stage": "other", "tier": "regular",
            "publish_weekday": "Monday"}
    base.update(over)
    return base


def test_features_from_row():
    feats = features_from_row(_row())
    assert feats["player"] == "frozen"
    assert feats["opp_tier"] == "top20"
    assert feats["rating"] == "1.2+"
    assert feats["kd"] == "1.5+"
    assert feats["derby"] == "cold"
    assert feats["weekday"] == "Monday"


def test_predict_sums_groups():
    weights = new_weights()
    weights["bias"] = 2.0
    weights["player"]["frozen"] = 0.5
    assert predict_log_views(features_from_row(_row()), weights) == 2.5


def test_sgd_epoch_learns_and_zero_alpha_freezes():
    from shorts.fit_pov_weights import GROUPS
    weights = new_weights()
    alphas = {group: 0.0 for group in GROUPS}
    alphas.update({"map": 0.1, "bias": 0.0, "channel": 0.0})
    sgd_epoch([_row(target_views=100000)], weights, {}, alphas=alphas)
    assert weights["player"] == {}
    assert weights["map"]["mirage"] > 0


def test_spearman_reexport():
    assert spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0


def test_time_split_is_ordered_and_disjoint():
    from shorts.fit_pov_weights import time_split
    rows = [{"published_at": f"2026-01-{i:02d}", "i": i} for i in range(1, 11)]
    train, val, test = time_split(rows, train_frac=0.8, val_of_tail=0.5)
    assert [r["i"] for r in train] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [r["i"] for r in val] == [9]
    assert [r["i"] for r in test] == [10]


def test_row_from_card_matches_dataset_features():
    from shorts.fit_pov_weights import features_from_row, row_from_card
    row = _row()
    card = {"player": "Frozen", "map": "Mirage", "rating": 1.2, "kd": 1.5,
            "decider": "no", "won": "yes", "ot": "no", "derby_views": 5000,
            "stage": "other", "tournament": "CCT Season 3",
            "publish_weekday": "Monday"}
    feats = features_from_row(row_from_card(
        card, "FaZe", "PARIVISION", {"PARIVISION": 18}, derby_views=5000))
    expect = features_from_row(row)
    for key in expect:
        if key == "_channel":
            continue
        assert feats[key] == expect[key], key


def test_serve_uses_rating_group():
    from hltv.score_cards import predict_model_log_views
    from shorts.fit_pov_weights import new_weights
    weights = new_weights()
    weights["bias"] = 1.0
    weights["rating"]["1.2+"] = 0.5
    meta = {"player": "frozen", "map": "mirage", "rating": 1.2,
            "tournament": "online qualifier"}
    pred = predict_model_log_views(meta, "FaZe", "PARIVISION", weights, {})
    assert pred == 1.5


def test_within_match_pairwise():
    from shorts.fit_pov_weights import evaluate_within_match, new_weights
    weights = new_weights()
    weights["player"]["a"] = 1.0
    a = _row(player="a", target_views=100_000, published_at="2026-06-01T00:00:00")
    b = _row(player="b", target_views=1_000, published_at="2026-06-01T00:00:00")
    rank = evaluate_within_match([a, b], weights)
    assert rank["matches"] == 1
    assert rank["pairwise_acc"] == 1.0
    assert rank["top1_acc"] == 1.0


def test_label_views_plateaus_at_21_days():
    from shorts.fit_pov_weights import label_views
    mature = _row(target_views=2100, age_days=100)
    young = _row(target_views=700, age_days=7)
    assert label_views(mature) == 2100
    assert label_views(young) == 2100
    assert label_views(_row(target_views=1000)) == 1000


def test_event_tier_blast_open_and_qualifier():
    from shorts.pro_context import event_tier
    assert event_tier("BLAST Open Porto 2026") == "s-tier"
    assert event_tier("IEM Krakow 2026") == "s-tier"
    assert event_tier("IEM Atlanta 2026 Closed Qualifier") == "regular"
    assert event_tier("PGL Major 2026") == "major"


def test_parse_navi_vs_3dmax_not_self():
    from shorts.pro_context import parse_pro_title, same_team
    parsed = parse_pro_title(
        "Aleksib (16-8) NAVI vs 3DMAX (Ancient) ESL Pro League #navi")
    assert parsed["team1"] == "Natus Vincere"
    assert parsed["team2"] == "3DMAX"
    assert same_team("NaVi", "Natus Vincere")


def test_summarize_rounds_marks_ace_and_map_win():
    from shorts.demo_pov_features import summarize_rounds
    by_round = {1: {"zywoo": 5, "flamez": 1}, 2: {"zywoo": 2, "flamez": 0}}
    winners = {1: "CT", 2: "T"}
    sides = {(1, "zywoo"): "CT", (1, "flamez"): "T",
             (2, "zywoo"): "CT", (2, "flamez"): "T"}
    out = summarize_rounds(by_round, winners, sides)
    assert out["zywoo"]["multi"] == "ace"
    assert out["zywoo"]["won_map"] == "unknown"  # 1-1
    assert out["flamez"]["multi"] == "none"


def test_teams_from_folder_strips_event():
    from shorts.demo_pov_features import teams_from_folder, map_from_demo_name
    assert teams_from_folder("2396950-mouz-vs-vitality-blast-open-porto") == (
        "mouz", "vitality")
    assert teams_from_folder(
        "2396925-natus-vincere-vs-m80-blast-open-porto") == (
        "natus vincere", "m80")
    assert map_from_demo_name("mouz-vs-vitality-m2-mirage.dem") == "mirage"


def test_match_url_from_relative_hltv_href():
    from shorts.download_pov_demos import match_url_from_stats_html
    html = '<a href="/matches/2380123/nrg-vs-falcons-iem-krakow-2026">match</a>'
    assert match_url_from_stats_html(html) == (
        "https://www.hltv.org/matches/2380123/nrg-vs-falcons-iem-krakow-2026")


def test_match_url_skips_short_related_match_href():
    from shorts.download_pov_demos import match_url_from_stats_html
    html = (
        '<a href="/matches/122371/nrg-vs-falcons">old</a>'
        '<a href="/matches/2389652/nrg-vs-falcons-iem-krakw-2026">real</a>'
        '<a href="/matches/2396951/spirit-vs-mouz-blast-open-porto-2026">other</a>'
    )
    stats = "https://www.hltv.org/stats/matches/mapstatsid/218121/nrg-vs-falcons"
    assert match_url_from_stats_html(html, stats) == (
        "https://www.hltv.org/matches/2389652/nrg-vs-falcons-iem-krakw-2026")
