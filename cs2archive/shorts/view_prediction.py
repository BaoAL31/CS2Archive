"""Shared log-view predictor; exp(log prediction) means typical Allstar views.

It is a geometric prediction, not an estimate of arithmetic expected views.
Unknown categorical levels contribute zero, just as they do in the fitted
design matrix. Production cuts use the saved reference age and Allstar source
unless explicitly supplied; this is an Allstar proxy, not our YouTube views.
"""
from __future__ import annotations

import math

MODEL_VERSION = 2


def predict_log_views(row: dict, model: dict) -> float:
    score = float(model["intercept"])
    for factor, key in (("player", "steamid"), ("opponent", "opponent"),
                        ("stage", "stage"), ("source", "source")):
        value = str(row.get(key) or "")
        score += float((model.get(factor) or {}).get(value, 0.0))
    for kind in set(row.get("kinds") or []):
        score += float((model.get("kind") or {}).get(kind, 0.0))
    age = float(row.get("age_days") or 0.0)
    if not math.isfinite(age) or age < 0:
        raise ValueError("age_days must be finite and nonnegative")
    score += float(model.get("clip_age") or 0.0) * age
    if not math.isfinite(score):
        raise ValueError("nonfinite view prediction")
    return score


def predicted_views(row: dict, model: dict) -> float:
    result = math.exp(predict_log_views(row, model))
    if not math.isfinite(result):
        raise ValueError("view prediction overflow")
    return result


def cut_features(short: dict, model: dict, *, orgs: list[str] | None = None) -> dict:
    from cs2archive.shorts.clip_observation import kinds_from_cut, opponent_of_cut, parse_stage

    stage = short.get("stage") or short.get("match_stage")
    if stage not in {"group", "playoff", "grand_final"}:
        stage = parse_stage(stage)
    kinds = short.get("kinds")
    if not isinstance(kinds, (list, tuple)):
        kinds = kinds_from_cut(short)
    return {
        "steamid": str(short.get("pov_steam_id") or ""),
        "opponent": opponent_of_cut(short, orgs),
        "stage": stage,
        "kinds": list(dict.fromkeys(kinds)),
        "source": short.get("source") or "allstar",
        "age_days": short.get("age_days", short.get("clip_age", model.get("reference_age_days", 0))),
    }


def validate_model(model: dict) -> None:
    if model.get("model_version") != MODEL_VERSION:
        raise ValueError("shorts model is incomplete/obsolete; run fit_partial_stars --allstar-only")
    for factor in ("player", "opponent", "stage", "kind", "source"):
        if not isinstance(model.get(factor), dict):
            raise ValueError(f"missing model factor: {factor}")
        if any(not math.isfinite(float(v)) for v in model[factor].values()):
            raise ValueError(f"nonfinite model factor: {factor}")
    for scalar in ("intercept", "clip_age", "reference_age_days"):
        if not math.isfinite(float(model[scalar])):
            raise ValueError(f"nonfinite model scalar: {scalar}")
    if int(model.get("training_rows", 0)) < 2:
        raise ValueError("shorts model has insufficient training rows")
