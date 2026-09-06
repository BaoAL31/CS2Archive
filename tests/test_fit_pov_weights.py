"""Seams for the POV weight fitter (LIM pro channel)."""
from __future__ import annotations

from shorts.fit_clip_weights import spearman
from shorts.fit_pov_weights import features_from_pov, predict_log_vpd, sgd_epoch


def _row(player="frozen", title="frozen POV with Keystrokes (15-7) FaZe vs PARIVISION (mirage) PGL Cluj-Napoca 2026",
         channel="LIM-CS POV | Pro Tournaments", vpd=1000.0,
         published_at="2026-06-01T00:00:00+00:00"):
    return {"primary_player": player, "title": title, "channel": channel,
            "views_per_day": str(vpd), "map": "mirage",
            "published_at": published_at, "views": "10000"}


def test_features_pro_context():
    feats = features_from_pov(_row(), org_of=lambda p: "FaZe")
    assert feats["player"] == "frozen"
    assert feats["org"] == "FaZe"
    assert feats["map"] == "mirage"
    assert feats["opp"] == "PARIVISION"
    assert feats["stage"] == "other"
    assert feats["tier"] == "regular"


def test_features_stage_and_major():
    feats = features_from_pov(_row(
        title="donk (25-11) Spirit vs FaZe (Dust2) IEM Cologne Major 2026 Grand Final"),
        org_of=lambda p: "Spirit")
    assert feats["stage"] == "final"
    assert feats["tier"] == "major"
    assert feats["opp"] == "FaZe"


def test_predict_sums_all_groups():
    weights = {"bias": 2.0, "player": {"frozen": 0.5}, "org": {},
               "map": {}, "opp": {}, "opp_tier": {}, "rating": {},
               "stage": {}, "tier": {}}
    feats = features_from_pov(_row())
    feats["_channel"] = "LIM-CS POV | Pro Tournaments"
    assert predict_log_vpd(feats, weights,
                           channel_bias={"LIM-CS POV | Pro Tournaments": 1.0},
                           org_of=lambda p: None) == 3.5


def test_sgd_epoch_alpha_zero_freezes_group():
    from shorts.fit_pov_weights import new_weights
    weights = new_weights()
    bias: dict = {}
    letters = "abcdefghijklmnopqrstuvwxyz"
    alphas = {"player": 0.0, "org": 0.0, "map": 0.1, "opp": 0.0,
              "opp_tier": 0.0, "rating": 0.0, "stage": 0.0, "tier": 0.0,
              "bias": 0.0, "channel": 0.0}
    assert all(weights[g] == {} for g in
               ("player", "org", "map", "opp", "opp_tier", "rating",
                "stage", "tier"))
    _ = letters
    sgd_epoch([_row(vpd=100000.0)], weights, bias,
              alphas=alphas, org_of=lambda p: None)
    assert weights["player"] == {}
    assert weights["map"]["mirage"] > 0


def test_spearman_reexport():
    assert spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 1.0
