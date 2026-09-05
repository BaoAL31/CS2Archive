"""Seams for the clip-weight fitter (views ~ kind + player + org + weapon)."""
from __future__ import annotations

import math

from shorts.fit_clip_weights import (
    features_from_clip,
    predict_log_views,
    spearman,
    sgd_epoch,
)


def _clip(title="AK-47 4K on Inferno", player="NiKo", views=22523):
    return {"title": title, "label": f"{player} {title}",
            "player": player, "views": views}


def test_features_primary_kind_prefers_clutch_over_multikill():
    feats = features_from_clip(_clip("1v3 4K Clutch"))
    assert feats["kind"] == "1v3_won"


def test_features_falls_back_to_multikill_then_stack():
    assert features_from_clip(_clip("AK-47 4K on Inferno"))["kind"] == "4k"
    assert features_from_clip(_clip("Wallbang 3K"))["kind"] == "3k"
    assert features_from_clip(_clip("Wallbang 3K"))["wallbang"] == 1
    assert features_from_clip(_clip("Desert Eagle 3K"))["weapon"] == "deagle"


def test_features_unknown_weapon_is_none():
    assert features_from_clip(_clip("Crazy round"))["weapon"] == "other"


def test_predict_sums_bias_and_feature_weights():
    weights = {"bias": 3.0, "kind": {"4k": 0.5}, "player": {},
               "org": {}, "weapon": {}, "wallbang": 0.0}
    feats = features_from_clip(_clip("AK-47 4K on Inferno"))
    assert predict_log_views(feats, weights, org_of=lambda p: None) == \
        3.0 + 0.5 + 0.0  # weapon ak47 unseen -> 0


def test_sgd_epoch_nudges_underpredicted_features_up():
    weights = {"bias": 3.0, "kind": {}, "player": {}, "org": {},
               "weapon": {}, "wallbang": 0.0}
    clips = [_clip("AK-47 4K on Inferno", views=100000)]  # log10 = 5
    match_bias = {"m1": 0.0}
    sgd_epoch([("m1", clips)], weights, match_bias,
              alphas={"kind": 0.1, "player": 0.1, "org": 0.1,
                      "weapon": 0.1, "bias": 0.0, "match": 0.1},
              org_of=lambda p: None)
    assert weights["kind"]["4k"] > 0
    assert match_bias["m1"] >= 0  # match intercept absorbs, feature keeps some


def test_sgd_epoch_alpha_zero_freezes_group():
    weights = {"bias": 3.0, "kind": {}, "player": {}, "org": {},
               "weapon": {}, "wallbang": 0.0}
    clips = [_clip("AK-47 4K on Inferno", views=100000)]
    match_bias = {"m1": 0.0}
    sgd_epoch([("m1", clips)], weights, match_bias,
              alphas={"kind": 0.0, "player": 0.0, "org": 0.0,
                      "weapon": 0.0, "bias": 0.0, "match": 0.1},
              org_of=lambda p: None)
    assert weights["kind"] == {}
    assert match_bias["m1"] > 0  # only match bias learned


def test_spearman_perfect_and_inverse():
    assert spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == 1.0
    assert spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == -1.0


def test_spearman_ignores_ties_gracefully():
    assert math.isfinite(spearman([1.0, 1.0, 2.0], [5.0, 6.0, 7.0]))
