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
