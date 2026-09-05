"""Seams for the POV weight fitter (views ~ player + org + map + elo)."""
from __future__ import annotations

from shorts.fit_clip_weights import spearman
from shorts.fit_pov_weights import features_from_pov, predict_log_vpd, sgd_epoch


def _row(player="ropz", channel="CS2 Archive", vpd=1000.0, map="inferno",
         elo="", published_at="2026-06-01T00:00:00+00:00"):
    return {"primary_player": player, "channel": channel,
            "views_per_day": str(vpd), "map": map, "elo": elo,
            "published_at": published_at, "views": "10000"}


def test_features_player_org_map():
    feats = features_from_pov(_row(), org_of=lambda p: "Vitality")
    assert feats["player"] == "ropz"
    assert feats["org"] == "Vitality"
    assert feats["map"] == "inferno"


def test_features_elo_bucketed_missing_is_none():
    assert features_from_pov(_row())["elo"] == "none"
    assert features_from_pov(_row(elo="5200"))["elo"] == "5k+"
    assert features_from_pov(_row(elo="3100"))["elo"] == "3k"


def test_predict_sums_channel_bias_and_weights():
    weights = {"bias": 2.0, "player": {"ropz": 0.5}, "org": {},
               "map": {}, "elo": {}}
    feats = features_from_pov(_row())
    feats["_channel"] = "CS2 Archive"
    assert predict_log_vpd(feats, weights, channel_bias={"CS2 Archive": 1.0},
                           org_of=lambda p: None) == 3.5


def test_sgd_epoch_alpha_zero_freezes_group():
    weights = {"bias": 2.0, "player": {}, "org": {}, "map": {}, "elo": {}}
    bias = {"CS2 Archive": 0.0}
    sgd_epoch([_row(vpd=100000.0)], weights, bias,
              alphas={"player": 0.0, "org": 0.0, "map": 0.1,
                      "elo": 0.0, "bias": 0.0, "channel": 0.1},
              org_of=lambda p: None)
    assert weights["player"] == {}
    assert weights["map"]["inferno"] > 0


def test_spearman_reexport():
    assert spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0
