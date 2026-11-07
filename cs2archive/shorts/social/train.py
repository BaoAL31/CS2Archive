"""Experimental model sweep over the social-evidence database.

Hard rules baked into the code:

* **Verified short-format rows only.** A row enters a model only when its format
  is independently verified (YouTube ``/shorts/`` route with a Shorts marker,
  Instagram Reel permalink) at confidence >= 0.8. Duration-only guesses and
  unverified clip artifacts are excluded from training and from eligibility.
* Per-platform models. Raw view/like counts from different platforms are never
  pooled into one target.
* Pre-publication inputs only. Outcome-named features are rejected outright, and
  measurement-time features (``age_hours_at_observation``) never enter the
  matrix.
* Cohort treatment is fit on **training rows only**; test rows with an unseen or
  thin cohort fall back to the training-wide median, counted explicitly.
* Holdout: chronological cut, then training rows whose identity group also
  appears in the test block are purged. Groups are connected components over
  verified clip *and* match identities.
* If identity coverage is too low, evaluation is refused (or explicitly labelled
  restricted when the operator opts in). Never silently called credible.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from cs2archive.shorts.social.config import SweepConfig
from cs2archive.shorts.social.identity import assign_groups
from cs2archive.shorts.social.report import TARGET_METRIC
from cs2archive.shorts.social.store import SocialStore

FORBIDDEN_FEATURE_TOKENS = (
    "views", "view_count", "plays", "likes", "like_count", "comments",
    "comment_count", "shares", "saves", "followers", "subscribers", "watch_time",
    "impressions", "ctr", "retention", "engagement", "score", "upvotes",
)
# Measurement-time variables: auditable in the DB, never model inputs.
MEASUREMENT_TIME_FEATURES = frozenset({"age_hours_at_observation"})
VERIFIED_FORMAT_MIN_CONFIDENCE = 0.8
TRAIN_FRAC = 0.8
MIN_TEST_ROWS = 10
MIN_COHORT_ROWS = 3
FEATURE_KINDS = ("gameplay", "presentation", "context")
CATEGORICAL_KINDS = {"creator_name": "context", "format_label": "presentation"}


@dataclass
class DatasetRow:
    platform: str
    post_id: str
    published_at: str
    cohort: str
    group_id: str
    group_basis: str
    target_raw: float
    features: dict = field(default_factory=dict)
    target: float | None = None
    cohort_seen: bool = False
    creator_id: str | None = None
    creator_name: str | None = None


def _is_forbidden(name: str) -> bool:
    low = name.lower()
    return any(token in low for token in FORBIDDEN_FEATURE_TOKENS)


def assert_no_leakage(feature_names) -> None:
    bad = sorted({name for name in feature_names if _is_forbidden(name)})
    if bad:
        raise ValueError(f"outcome metric used as a feature (leakage): {bad}")
    measurement = sorted({name for name in feature_names
                          if name in MEASUREMENT_TIME_FEATURES})
    if measurement:
        raise ValueError(f"measurement-time feature used as a predictor: {measurement}")


def _cohort(published_at: str) -> str:
    stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    iso_year, iso_week, _ = stamp.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def format_verification(snapshot) -> tuple[bool, str]:
    """A row may train only with independently verified short-form evidence."""
    label = str(snapshot["format_label"] or "unknown")
    confidence = float(snapshot["format_confidence"] or 0.0)
    try:
        evidence = json.loads(snapshot["format_evidence"] or "{}")
    except (TypeError, ValueError):
        evidence = {}
    if label != "short":
        return False, f"format={label}"
    if confidence < VERIFIED_FORMAT_MIN_CONFIDENCE:
        return False, f"format confidence {confidence:.2f}"
    if evidence.get("verified") is not True:
        return False, "format evidence not independently verified"
    return True, ""


def load_platform_rows(store: SocialStore, platform: str) -> tuple[list[DatasetRow], dict]:
    links_by_post: dict[tuple[str, str], list[dict]] = {}
    for link in store.conn.execute(
            "SELECT * FROM clip_identity_links WHERE platform=?", (platform,)):
        links_by_post.setdefault((link["platform"], link["post_id"]), []).append(
            dict(link))
    feature_rows: dict[tuple[str, str], dict[str, float]] = {}
    for feature in store.conn.execute(
            "SELECT platform, post_id, feature, value_num FROM post_features "
            "WHERE platform=? AND value_num IS NOT NULL", (platform,)):
        feature_rows.setdefault((feature["platform"], feature["post_id"]), {})[
            feature["feature"]] = float(feature["value_num"])

    quarantined = store.quarantined_posts()
    target_metric = TARGET_METRIC.get(platform)
    stats = {"platform": platform, "target_metric": target_metric,
             "candidates": 0, "unverified_format": 0, "format_rejections": {},
             "missing_target": 0, "zero_engagement": 0,
             "missing_published_at": 0, "quarantined": 0, "kept": 0}
    rows: list[DatasetRow] = []
    for snapshot in store.latest_snapshots():
        if snapshot["platform"] != platform:
            continue
        stats["candidates"] += 1
        key = (snapshot["platform"], snapshot["post_id"])
        if key in quarantined:
            stats["quarantined"] += 1
            continue
        if not target_metric:
            stats["missing_target"] += 1
            continue
        verified, reason = format_verification(snapshot)
        if not verified:
            stats["unverified_format"] += 1
            stats["format_rejections"][reason] = \
                stats["format_rejections"].get(reason, 0) + 1
            continue
        value = snapshot[target_metric]
        if value is None:
            stats["missing_target"] += 1
            continue
        value = float(value)
        if value < 0:  # platform counts are never negative; treat as unavailable
            stats["missing_target"] += 1
            continue
        if value == 0:
            # Zero engagement is real data, kept and modelled with log1p.
            stats["zero_engagement"] += 1
        if not snapshot["published_at"]:
            stats["missing_published_at"] += 1
            continue
        features = {name: value for name, value in feature_rows.get(key, {}).items()
                    if not _is_forbidden(name) and name not in MEASUREMENT_TIME_FEATURES}
        assert_no_leakage(features)
        if snapshot["creator_name"]:
            features["creator_name"] = str(snapshot["creator_name"])
        if snapshot["format_label"]:
            features["format_label"] = str(snapshot["format_label"])
        rows.append(DatasetRow(
            platform=platform, post_id=snapshot["post_id"],
            creator_id=snapshot["creator_id"],
            creator_name=snapshot["creator_name"],
            published_at=str(snapshot["published_at"]),
            cohort=_cohort(str(snapshot["published_at"])),
            group_id="", group_basis="", target_raw=value, features=features))

    groups = assign_groups([(row.platform, row.post_id) for row in rows], links_by_post)
    for row in rows:
        group = groups[(row.platform, row.post_id)]
        row.group_id = group["group_id"]
        row.group_basis = group["basis"]
    stats["kept"] = len(rows)
    stats["verified_identity_rows"] = sum(
        1 for row in rows if row.group_basis == "verified")
    stats["unverified_identity_rows"] = len(rows) - stats["verified_identity_rows"]
    stats["verified_identity_share"] = (
        stats["verified_identity_rows"] / len(rows) if rows else 0.0)
    return rows, stats


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def fit_cohort_targets(train: list[DatasetRow],
                       test: list[DatasetRow]) -> dict:
    """Fit cohort medians on training rows only, then apply them to both sets.

    Held-out outcomes never influence a training target: a test row in an unseen
    or thin cohort uses the training-wide median and is counted as a fallback.
    """
    buckets: dict[str, list[float]] = {}
    for row in train:
        buckets.setdefault(row.cohort, []).append(math.log1p(row.target_raw))
    medians = {cohort: _median(values) for cohort, values in buckets.items()
               if len(values) >= MIN_COHORT_ROWS}
    train_logs = [math.log1p(row.target_raw) for row in train]
    global_median = _median(train_logs) if train_logs else 0.0
    for row in train:
        median = medians.get(row.cohort, global_median)
        row.cohort_seen = row.cohort in medians
        row.target = round(math.log1p(row.target_raw) - median, 6)
    fallbacks = 0
    for row in test:
        median = medians.get(row.cohort)
        row.cohort_seen = median is not None
        if median is None:
            fallbacks += 1
            median = global_median
        row.target = round(math.log1p(row.target_raw) - median, 6)
    return {"cohorts_fitted": len(medians), "train_cohorts": len(buckets),
            "test_cohort_fallbacks": fallbacks,
            "global_train_median": round(global_median, 6)}


def split_rows(rows: list[DatasetRow], train_frac: float = TRAIN_FRAC) -> dict:
    ordered = sorted(rows, key=lambda row: (row.published_at, row.post_id))
    cut = int(len(ordered) * train_frac)
    train = ordered[:cut]
    test = ordered[cut:]
    test_groups = {row.group_id for row in test}
    purged = [row for row in train if row.group_id in test_groups]
    train = [row for row in train if row.group_id not in test_groups]
    return {"train": train, "test": test, "purged": len(purged),
            "train_groups": len({row.group_id for row in train}),
            "test_groups": len(test_groups)}


def _matrix(features_train: list[dict], features_test: list[dict]):
    """Train-only preprocessing: the vectorizer is fit on training rows only."""
    from sklearn.feature_extraction import DictVectorizer

    vectorizer = DictVectorizer(sparse=False)
    mat_train = vectorizer.fit_transform(features_train)
    mat_test = vectorizer.transform(features_test)
    return mat_train, mat_test, vectorizer


def _average_ranks(values) -> "object":
    """Competition-safe average ranks (ties share the mean rank)."""
    import numpy as np

    ordered = np.asarray(values, dtype=float)
    order = np.argsort(ordered, kind="mergesort")
    ranks = np.empty(len(ordered), dtype=float)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and ordered[order[j + 1]] == ordered[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def _spearman(pred, actual) -> float | None:
    import numpy as np

    if len(pred) < 3:
        return None
    rank_pred = _average_ranks(pred)
    rank_actual = _average_ranks(actual)
    if rank_pred.std() == 0 or rank_actual.std() == 0:
        return None
    return float(np.corrcoef(rank_pred, rank_actual)[0, 1])


def _top_k_indices(values, k: int) -> list[int]:
    """Deterministic, tie-aware top-k: value desc, original index as tie-break."""
    import numpy as np

    array = np.asarray(values, dtype=float)
    order = np.lexsort((np.arange(len(array)), -array))
    return sorted(order[:k].tolist())


def _precision_at_k(pred, actual, k: int) -> float | None:
    n = len(pred)
    if n == 0:
        return None
    k = max(1, min(k, n))
    top_pred = set(_top_k_indices(pred, k))
    top_actual = set(_top_k_indices(actual, k))
    return len(top_pred & top_actual) / k


def _models():
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "ridge": lambda: Pipeline([
            ("scale", StandardScaler(with_mean=False)),
            ("model", Ridge(alpha=10.0, random_state=7)),
        ]),
        "hgb": lambda: HistGradientBoostingRegressor(
            max_iter=200, learning_rate=0.06, max_depth=4, min_samples_leaf=5,
            l2_regularization=1.0, random_state=7),
    }


def _ablations() -> dict[str, tuple[str, ...]]:
    return {
        "all": FEATURE_KINDS,
        "no_gameplay": ("presentation", "context"),
        "no_presentation": ("gameplay", "context"),
        "no_context": ("gameplay", "presentation"),
    }


def _kind_for(name: str) -> str:
    if name in CATEGORICAL_KINDS:
        return CATEGORICAL_KINDS[name]
    if name in {"clip_span_seconds", "round_span_seconds", "round_number",
                "first_action_seconds", "dead_time_seconds", "duration_seconds"}:
        return "presentation"
    if name.startswith(("caption_", "published_", "age_", "creator_")):
        return "context"
    return "gameplay"


def _feature_kinds_map(rows: list[DatasetRow]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in rows:
        for name in row.features:
            mapping.setdefault(name, _kind_for(name))
    return mapping


def _features_for(row: DatasetRow, kinds: tuple[str, ...], kinds_by_name: dict,
                  universe: set[str] | None = None) -> dict:
    """Select ablation features and make missing values explicit.

    A feature that is genuinely absent becomes ``<name>__missing = 1`` instead of
    silently reading as an observed zero. Only the training universe is used, so
    the column set is learned from training rows.
    """
    selected = set(universe) if universe is not None else {
        name for name in row.features
        if name not in MEASUREMENT_TIME_FEATURES
        and kinds_by_name.get(name) in kinds}
    out: dict[str, float] = {}
    for name in selected:
        if name in MEASUREMENT_TIME_FEATURES or kinds_by_name.get(name) not in kinds:
            continue
        value = row.features.get(name)
        if value is None:
            out[f"{name}__missing"] = 1.0
        else:
            out[name] = value
    return out


def _feature_universe(rows: list[DatasetRow], kinds: tuple[str, ...],
                      kinds_by_name: dict) -> set[str]:
    return {name for row in rows for name in row.features
            if name not in MEASUREMENT_TIME_FEATURES
            and kinds_by_name.get(name) in kinds}


def _creator_baseline(train: list[DatasetRow], test: list[DatasetRow],
                      *, fallback_value: float | None = None) -> dict:
    """Per-creator mean predictions with a scale-correct fallback.

    On an **absolute** scale (log1p views) an unseen creator must fall back to the
    training-wide median, not to 0.0 — 0.0 means "zero exposure", which is a
    fabricated prediction. On a cohort-relative target the median prediction is
    genuinely 0.0, so callers pass ``fallback_value=0.0``.
    """
    names_test = [str(row.features.get("creator_name") or "") for row in test]
    names_train = [str(row.features.get("creator_name") or "") for row in train]
    if not any(names_test):
        return {"skipped": "no creator feature on held-out rows"}
    if not any(names_train):
        return {"skipped": "no creator feature on training rows"}
    means = _creator_means(train)
    global_median = _median([row.target for row in train])
    fallback = global_median if fallback_value is None else float(fallback_value)
    predictions: list[float] = []
    fallbacks = 0
    for name in names_test:
        if name and name in means:
            predictions.append(means[name])
        else:
            predictions.append(fallback)
            fallbacks += 1
    return {
        "predictions": predictions,
        "fallback": ("training-wide median" if fallback_value is None
                     else f"constant {fallback}"),
        "fallback_value": round(fallback, 6),
        "fallback_count": fallbacks,
        "global_median": round(global_median, 6),
    }


def _creator_means(train: list[DatasetRow]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for row in train:
        name = str(row.features.get("creator_name") or "")
        if name and row.target is not None:
            buckets.setdefault(name, []).append(row.target)
    return {name: sum(values) / len(values) for name, values in buckets.items()}


def _score(pred, actual, k: int) -> dict:
    import numpy as np

    pred = np.asarray(pred, dtype=float)
    actual = np.asarray(actual, dtype=float)
    return {
        "n": int(len(actual)),
        "mae": round(float(np.mean(np.abs(pred - actual))), 5),
        "spearman": (round(value, 4) if (value := _spearman(pred, actual)) is not None
                     else None),
        "precision_at_k": _precision_at_k(pred, actual, k),
        "k": k,
    }


def _age_diagnostic(test: list[DatasetRow], pred, actual) -> dict:
    import numpy as np

    pairs = [(float(row.features["age_hours_at_observation"]), float(a - p))
             for row, p, a in zip(test, pred, actual)
             if "age_hours_at_observation" in row.features]
    if len(pairs) < 5:
        return {"n": len(pairs), "residual_vs_age_spearman": None,
                "note": "measurement age is excluded from predictors"}
    ages = [age for age, _ in pairs]
    residuals = [residual for _, residual in pairs]
    return {"n": len(pairs),
            "median_age_hours": round(float(np.median(ages)), 2),
            "residual_vs_age_spearman": (
                round(value, 4) if (value := _spearman(ages, residuals)) is not None
                else None),
            "note": "measurement age is a diagnostic only, never a predictor"}


def evaluate_platform(rows: list[DatasetRow], *, platform: str, min_rows: int,
                      allow_unverified_groups: bool = False) -> dict:
    verified_rows = [row for row in rows if row.group_basis == "verified"]
    excluded_unverified = len(rows) - len(verified_rows)
    if allow_unverified_groups:
        scored_rows = rows
        identity_status = "unverified_risk"
    else:
        # Restrict the scored population to verified identities rather than
        # claiming a leakage-free evaluation from an arbitrary coverage ratio.
        scored_rows = verified_rows
        identity_status = "verified_only"
    if len(scored_rows) < min_rows:
        return {"platform": platform, "status": "refused",
                "reason": f"only {len(scored_rows)} usable rows with "
                          f"{identity_status} identity (minimum {min_rows}); "
                          "no model was fit",
                "identity": {"mode": identity_status,
                             "rows": len(rows), "scored_rows": len(scored_rows),
                             "excluded_unverified": excluded_unverified}}
    split = split_rows(scored_rows)
    train, test = split["train"], split["test"]
    targets = fit_cohort_targets(train, test)
    if len(test) < MIN_TEST_ROWS or split["train_groups"] < 2:
        return {"platform": platform, "status": "refused",
                "reason": f"insufficient holdout data (train={len(train)}, "
                          f"test={len(test)}, groups={split['train_groups']}); "
                          "no credible out-of-sample score",
                "identity": {"mode": identity_status, "rows": len(rows),
                             "scored_rows": len(scored_rows),
                             "excluded_unverified": excluded_unverified},
                "split": {"train": len(train), "test": len(test),
                          "purged": split["purged"]}}
    import numpy as np

    kinds_by_name = _feature_kinds_map(scored_rows)
    actual = np.array([row.target for row in test])
    k = max(1, min(5, len(test) // 10 or 1))
    results: dict[str, dict] = {}

    results["baseline_cohort_median"] = {
        **_score(np.zeros(len(test)), actual, k),
        "note": "cohort-relative target => the cohort median predicts 0.0 by "
                "construction",
    }
    creator_baseline = _creator_baseline(train, test, fallback_value=0.0)
    if "skipped" in creator_baseline:
        results["baseline_creator_median"] = {"skipped": creator_baseline["skipped"]}
    else:
        score = _score(np.array(creator_baseline["predictions"]), actual, k)
        score["fallback"] = creator_baseline["fallback"]
        score["fallback_count"] = creator_baseline["fallback_count"]
        score["note"] = ("cohort-relative target: unseen creators fall back to the "
                         "cohort-median prediction 0.0")
        results["baseline_creator_median"] = score

    models = _models()
    for ablation, kinds in _ablations().items():
        universe = _feature_universe(train, kinds, kinds_by_name)
        train_features = [_features_for(row, kinds, kinds_by_name, universe)
                          for row in train]
        test_features = [_features_for(row, kinds, kinds_by_name, universe)
                         for row in test]
        used_names = sorted({name for features in train_features for name in features})
        assert_no_leakage(used_names)
        mat_train, mat_test, _vectorizer = _matrix(train_features, test_features)
        for model_name, factory in models.items():
            label = f"{model_name}:{ablation}"
            try:
                model = factory()
                model.fit(mat_train, np.array([row.target for row in train]))
                pred = model.predict(mat_test)
                score = _score(np.asarray(pred), actual, k)
                score["features"] = int(mat_train.shape[1])
                score["feature_names"] = used_names
                if ablation == "all":
                    score["age_diagnostic"] = _age_diagnostic(test, pred, actual)
                results[label] = score
            except Exception as exc:  # noqa: BLE001 - a failed model is reported
                results[label] = {"error": f"{type(exc).__name__}: {exc}"}
    warnings: list[str] = []
    if len(train) < 30 or len(test) < 10:
        warnings.append(f"small sample (train={len(train)}, test={len(test)}): "
                        "metrics are unstable and are not evidence of a production effect")
    present_features = _feature_kinds_map(train)
    if not any(_kind_for(name) == "gameplay" for name in present_features):
        warnings.append("no measured gameplay features for this platform: the "
                        "gameplay ablation is identical to the full model")
    if excluded_unverified:
        warnings.append(f"{excluded_unverified} rows were excluded because their "
                        "identity is unverified (ambiguous/unlinked)")
    if targets["test_cohort_fallbacks"]:
        warnings.append(f"{targets['test_cohort_fallbacks']} held-out rows had an "
                        "unseen/thin cohort and used the training-wide median; their "
                        "scores are not fully age-normalised")
    return {
        "platform": platform,
        "status": "fitted" if not allow_unverified_groups else "restricted",
        "warnings": warnings,
        "identity": {"mode": identity_status, "rows": len(rows),
                     "scored_rows": len(scored_rows),
                     "verified_rows": len(verified_rows),
                     "excluded_unverified": excluded_unverified},
        "target": {
            "metric": TARGET_METRIC.get(platform),
            "transform": "log1p",
            "definition": "log1p(metric) - train-fitted cohort median "
                          "(training-wide median for unseen/thin cohorts); zero "
                          "engagement is kept",
            "note": "cumulative observed count at observation time - not a historical "
                    "24h figure, and not a causal effect",
        },
        "split": {"train": len(train), "test": len(test), "purged": split["purged"],
                  "train_groups": split["train_groups"],
                  "test_groups": split["test_groups"]},
        "targets": targets,
        "results": results,
        "ablations": list(_ablations()),
    }


def load_allstar_rows(store: SocialStore) -> tuple[list[DatasetRow], dict]:
    """Allstar gameplay rows: verified clip identity, no format/date requirement."""
    links_by_post: dict[tuple[str, str], list[dict]] = {}
    for link in store.conn.execute(
            "SELECT * FROM clip_identity_links WHERE platform='allstar'"):
        links_by_post.setdefault(("allstar", link["post_id"]), []).append(dict(link))
    features: dict[str, dict[str, float]] = {}
    for row in store.conn.execute(
            "SELECT post_id, feature, value_num FROM post_features "
            "WHERE platform='allstar' AND value_num IS NOT NULL"):
        features.setdefault(row["post_id"], {})[row["feature"]] = float(row["value_num"])
    quarantined = store.quarantined_posts()
    stats = {"candidates": 0, "missing_views": 0, "quarantined": 0,
             "no_measured_gameplay": 0, "kept": 0, "zero_engagement": 0}
    raw_rows: list[DatasetRow] = []
    posts = [(row["platform"], row["post_id"])
             for row in store.conn.execute("SELECT platform, post_id FROM posts "
                                           "WHERE platform='allstar'")]
    groups = assign_groups(posts, links_by_post)
    for snapshot in store.latest_snapshots():
        if snapshot["platform"] != "allstar":
            continue
        stats["candidates"] += 1
        post_id = snapshot["post_id"]
        if ("allstar", post_id) in quarantined:
            stats["quarantined"] += 1
            continue
        views = snapshot["views"]
        if views is None:
            stats["missing_views"] += 1
            continue
        measured = {name: value for name, value in features.get(post_id, {}).items()
                    if not _is_forbidden(name) and name not in MEASUREMENT_TIME_FEATURES}
        if snapshot["creator_name"]:
            measured["creator_name"] = str(snapshot["creator_name"])
        if "kills" not in measured:
            stats["no_measured_gameplay"] += 1
            continue
        if float(views) == 0:
            stats["zero_engagement"] += 1
        info = groups[("allstar", post_id)]
        raw_rows.append(DatasetRow(
            platform="allstar", post_id=post_id, published_at="",
            cohort="allstar-no-dates", group_id=info["group_id"],
            group_basis=info["basis"], target_raw=float(views),
            features=measured))
        stats["kept"] += 1
    return raw_rows, stats


def _allstar_ablation_features() -> dict[str, set[str]]:
    return {
        "all": set(),
        "no_headshots": {"headshots", "headshot_rate"},
        "no_weapons": {"distinct_weapons", "fire_events", "hit_events",
                       "hit_event_ratio"},
        "density_only": set(),
    }


_ALLSTAR_DENSITY = {"kills", "kill_span_seconds", "kills_per_10s", "multikill",
                    "clip_span_seconds"}


def evaluate_allstar_gameplay(rows: list[DatasetRow], *, min_rows: int = 60) -> dict:
    """Allstar-only gameplay ranking. Match-held-out, no publication dates.

    This is deliberately NOT a forecast of YouTube performance: the label is the
    Allstar view count observed at import time, and there is no time axis. Groups
    are matches, so clips from one match never straddle train/test.
    """
    verified = [row for row in rows if row.group_basis == "verified"]
    if len(verified) < min_rows:
        return {"status": "refused",
                "reason": f"only {len(verified)} match-verified Allstar clips "
                          f"(minimum {min_rows})",
                "identity": {"rows": len(rows), "verified_rows": len(verified)}}
    from cs2archive.shorts.social.safety import sha256_text

    train = [row for row in verified
             if int(sha256_text(row.group_id)[:8], 16) % 100 < 80]
    test = [row for row in verified if row not in train]
    if len(train) < min_rows or len(test) < MIN_TEST_ROWS or             len({row.group_id for row in train}) < 5:
        return {"status": "refused",
                "reason": f"match-held-out split too small (train={len(train)}, "
                          f"test={len(test)})",
                "split": {"train": len(train), "test": len(test)}}
    import numpy as np

    for row in train:
        row.target = math.log1p(row.target_raw)
    for row in test:
        row.target = math.log1p(row.target_raw)
    kinds_by_name = _feature_kinds_map(verified)
    actual = np.array([row.target for row in test])
    k = max(1, min(5, len(test) // 10 or 1))
    results: dict[str, dict] = {}
    global_median = _median([row.target for row in train])
    results["baseline_global_median"] = _score(
        np.full(len(test), global_median), actual, k)
    creator_baseline = _creator_baseline(train, test)
    if "skipped" in creator_baseline:
        results["baseline_creator_median"] = {"skipped": creator_baseline["skipped"]}
    else:
        score = _score(np.array(creator_baseline["predictions"]), actual, k)
        score["fallback"] = creator_baseline["fallback"]
        score["fallback_value"] = creator_baseline["fallback_value"]
        score["fallback_count"] = creator_baseline["fallback_count"]
        score["note"] = ("absolute log1p scale: unseen creators use "
                         f"{creator_baseline['fallback']}, never 0.0")
        results["baseline_creator_median"] = score

    kinds = ("gameplay", "presentation")
    universe_all = _feature_universe(train, kinds, kinds_by_name)
    models = _models()
    for ablation, dropped in _allstar_ablation_features().items():
        universe = {name for name in universe_all if name not in dropped}
        if ablation == "density_only":
            universe = {name for name in universe if name in _ALLSTAR_DENSITY}
        train_features = [_features_for(row, kinds, kinds_by_name, universe)
                          for row in train]
        test_features = [_features_for(row, kinds, kinds_by_name, universe)
                         for row in test]
        names = sorted({name for features in train_features for name in features})
        assert_no_leakage(names)
        mat_train, mat_test, _vec = _matrix(train_features, test_features)
        for model_name, factory in models.items():
            label = f"{model_name}:{ablation}"
            try:
                model = factory()
                model.fit(mat_train, np.array([row.target for row in train]))
                pred = model.predict(mat_test)
                score = _score(np.asarray(pred), actual, k)
                score["features"] = int(mat_train.shape[1])
                results[label] = score
            except Exception as exc:  # noqa: BLE001
                results[label] = {"error": f"{type(exc).__name__}: {exc}"}
    warnings = []
    if len(train) < 100:
        warnings.append(f"small Allstar sample (train={len(train)}); exploratory")
    warnings.append("label is Allstar views at observation, with no publication "
                    "dates: this is a gameplay ranking experiment, not a forecast "
                    "of YouTube/Instagram performance")
    return {
        "status": "fitted",
        "warnings": warnings,
        "label": {"name": "allstar_views_at_observation", "transform": "log1p",
                  "publication_dates": False,
                  "forecasts_youtube": False,
                  "note": "no date-based validation; match-held-out grouping"},
        "split": {"train": len(train), "test": len(test),
                  "train_matches": len({row.group_id for row in train}),
                  "test_matches": len({row.group_id for row in test}),
                  "method": "match_group_hash_80_20"},
        "results": results,
        "ablations": list(_allstar_ablation_features()),
    }


def analytics_diagnostics(store, *, own_channel_id: str,
                          min_rows: int) -> dict:
    """Owner-only analytics coverage, kept apart from the public views target."""
    rows = list(store.conn.execute(
        "SELECT a.post_id, a.window_start, a.window_end, a.impressions, a.ctr, "
        "a.average_view_percentage, a.views FROM analytics_snapshots a "
        "JOIN posts p ON p.platform = a.platform AND p.post_id = a.post_id "
        "WHERE a.platform='youtube' AND p.creator_id=?", (own_channel_id,)))
    from cs2archive.shorts.social.analytics import analytics_status

    status = analytics_status()
    if not rows:
        return {
            "status": "unavailable",
            "reason": status["reason"] or "no analytics rows ingested yet",
            "token_available": status["available"],
            "rows": 0,
            "used_as_target": False,
            "note": "analytics is a separate family; it never becomes the public "
                    "views target and is never merged with it",
        }
    with_ctr = [row for row in rows if row["ctr"] is not None]
    return {
        "status": "available" if len(rows) >= min_rows else "insufficient",
        "token_available": status["available"],
        "rows": len(rows),
        "posts": len({row["post_id"] for row in rows}),
        "with_ctr": len(with_ctr),
        "windows": sorted({f"{row['window_start']}..{row['window_end']}"
                           for row in rows}),
        "median_impressions": _median([float(row["impressions"]) for row in rows
                                       if row["impressions"] is not None]) or None,
        "median_ctr": _median([float(row["ctr"]) for row in with_ctr]) or None,
        "separate_family": "youtube_analytics",
        "used_as_target": False,
        "note": "analytics is a separate family; it never becomes the public views "
                "target and is never merged with it",
    }


def evaluate_own_channel_linked(rows: list[DatasetRow], *, own_channel_id: str,
                                min_rows: int) -> dict:
    """Own-channel Shorts that carry a *verified* demo link, plus analytics."""
    own = [row for row in rows if row.creator_id == own_channel_id]
    verified = [row for row in own if row.group_basis == "verified"]
    if len(verified) < min_rows:
        return {"status": "refused",
                "reason": f"only {len(verified)} own-channel Shorts have a verified "
                          f"demo link (minimum {min_rows}); no model was fit",
                "identity": {"own_posts": len(own), "verified_linked": len(verified),
                             "unlinked": len(own) - len(verified)}}
    return evaluate_platform(verified, platform="youtube_own_linked",
                             min_rows=min_rows)


def run_sweep(config: SweepConfig, *, experimental: bool = False,
              min_rows: int | None = None) -> dict:
    if not experimental:
        return {"status": "refused",
                "reason": "the sweep is experimental; re-run with --experimental"}
    try:
        import sklearn  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return {"status": "refused",
                "reason": f"scikit-learn unavailable ({exc}); no models were fit"}
    store = SocialStore(config.database, secrets=())
    try:
        minimum = int(min_rows or config.min_train_rows)
        allow_unverified = bool(
            (config.raw.get("training") or {}).get("allow_unverified_groups", False))
        platforms: dict[str, dict] = {}
        for platform in TARGET_METRIC:
            rows, stats = load_platform_rows(store, platform)
            if not rows:
                platforms[platform] = {"platform": platform, "status": "refused",
                                       "reason": "no verified short-format rows",
                                       "coverage": stats}
                continue
            platforms[platform] = {
                **evaluate_platform(rows, platform=platform, min_rows=minimum,
                                    allow_unverified_groups=allow_unverified),
                "coverage": stats}
        own_channel_id = str(
            (config.platform("youtube").extra or {}).get("own_channel_id") or "")
        own_linked = None
        own_rows, own_stats = load_platform_rows(store, "youtube")
        if own_channel_id:
            own_linked = evaluate_own_channel_linked(
                own_rows, own_channel_id=own_channel_id, min_rows=minimum)
            own_linked["coverage"] = {
                "verified_rows": own_stats.get("verified_identity_rows"),
                "rows": own_stats.get("kept"),
            }
            own_linked["analytics"] = analytics_diagnostics(
                store, own_channel_id=own_channel_id, min_rows=minimum)
        allstar_rows, allstar_stats = load_allstar_rows(store)
        allstar_gameplay = evaluate_allstar_gameplay(allstar_rows)
        allstar_gameplay["coverage"] = allstar_stats
        fitted = [name for name, result in platforms.items()
                  if result.get("status") in {"fitted", "restricted"}]
        if allstar_gameplay.get("status") == "fitted":
            fitted.append("allstar_gameplay")
        if own_linked and own_linked.get("status") == "fitted":
            fitted.append("youtube_own_linked")
        report = {
            "status": "fitted" if any(
                result.get("status") == "fitted"
                for result in platforms.values()) else "refused",
            "fitted_platforms": fitted,
            "reason": None if fitted else "no platform had enough verified rows",
            "platforms": platforms,
            "allstar_gameplay": allstar_gameplay,
            "youtube_own_linked": own_linked,
            "constraints": [
                "verified short-format rows only on the strict path (independent "
                "format evidence); Allstar clip artifacts are scored by the "
                "separate allstar_gameplay experiment instead",
                "per-platform models; raw cross-platform metrics are never pooled",
                "target is log1p(metric) relative to a cohort median fitted on "
                "training rows only (the separate allstar_gameplay experiment uses "
                "absolute log1p because Allstar has no publication dates)",
                "pre-publication features only; outcome and measurement-age features "
                "are excluded",
                "chronological holdout with connected-identity group purge",
                "the scored set is restricted to verified identities; rows with an "
                "unverified identity are excluded (or the residual leakage risk is "
                "labelled when allow_unverified_groups is set) - no coverage-ratio "
                "threshold is used to claim leakage-free evaluation",
                "no historical 24h performance and no causal claim",
            ],
        }
        output = Path(config.raw.get("training", {}).get(
            "output", str(config.database.parent / "train_report.json")))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                          encoding="utf-8")
        report["output"] = str(output)
        return report
    finally:
        store.close()


def format_sweep(report: dict) -> str:
    lines = [f"experimental sweep: {report['status']}"]
    if report.get("reason"):
        lines.append(f"  reason: {report['reason']}")
    own = report.get("youtube_own_linked")
    if own:
        lines.append(f"[youtube_own_linked] {own.get('status')}")
        if own.get("reason"):
            lines.append(f"  {own['reason']}")
        for warning in own.get("warnings") or []:
            lines.append(f"  warning: {warning}")
        if own.get("split"):
            lines.append(f"  split {own['split']}")
        for label, score in (own.get("results") or {}).items():
            if isinstance(score, dict) and "error" in score:
                lines.append(f"  {label:28s} FAILED {score['error']}")
            elif isinstance(score, dict):
                lines.append(f"  {label:28s} mae={score.get('mae')} "
                             f"spearman={score.get('spearman')} "
                             f"p@k={score.get('precision_at_k')}")
        analytics = own.get("analytics") or {}
        if analytics:
            lines.append(f"  analytics: {analytics.get('status')} "
                         f"rows={analytics.get('rows')} "
                         f"impressions={analytics.get('median_impressions')} "
                         f"ctr={analytics.get('median_ctr')} "
                         f"(separate family, never the target)")
    allstar = report.get("allstar_gameplay")
    if allstar:
        lines.append(f"[allstar_gameplay] {allstar.get('status')}")
        if allstar.get("reason"):
            lines.append(f"  {allstar['reason']}")
        for warning in allstar.get("warnings") or []:
            lines.append(f"  warning: {warning}")
        if allstar.get("split"):
            split = allstar["split"]
            lines.append(f"  split {split}")
        for label, score in (allstar.get("results") or {}).items():
            if "error" in score:
                lines.append(f"  {label:28s} FAILED {score['error']}")
                continue
            lines.append(f"  {label:28s} mae={score['mae']:.4f} "
                         f"spearman={score['spearman']} p@k={score['precision_at_k']}")
    for name, result in report.get("platforms", {}).items():
        lines.append(f"[{name}] {result.get('status')}")
        if result.get("reason"):
            lines.append(f"  {result['reason']}")
        if result.get("identity"):
            ident = result["identity"]
            verified = ident.get("verified_rows")
            rows_total = ident.get("rows")
            if verified is None or rows_total is None:
                lines.append(f"  identity mode={ident.get('mode')} "
                             f"scored_rows={ident.get('scored_rows')} "
                             f"excluded_unverified={ident.get('excluded_unverified')}")
            else:
                lines.append(f"  identity verified_share={ident.get('verified_share')} "
                             f"({verified}/{rows_total})")
        if result.get("coverage"):
            cov = result["coverage"]
            lines.append(f"  rows kept={cov.get('kept', 0)} "
                         f"unverified_format={cov.get('unverified_format', 0)} "
                         f"missing_target={cov.get('missing_target', 0)} "
                         f"missing_published_at={cov.get('missing_published_at', 0)} "
                         f"quarantined={cov.get('quarantined', 0)}")
            if cov.get("format_rejections"):
                lines.append(f"  format rejections: {cov['format_rejections']}")
        for warning in result.get("warnings") or []:
            lines.append(f"  warning: {warning}")
        if result.get("targets"):
            lines.append(f"  targets: {result['targets']}")
        if result.get("split"):
            split = result["split"]
            lines.append(f"  split train={split['train']} test={split['test']} "
                         f"purged={split['purged']} groups={split.get('train_groups')}")
        for label, score in (result.get("results") or {}).items():
            if "error" in score:
                lines.append(f"  {label:28s} FAILED {score['error']}")
                continue
            lines.append(
                f"  {label:28s} mae={score['mae']:.4f} "
                f"spearman={score['spearman']} p@k={score['precision_at_k']}")
    return "\n".join(lines)
