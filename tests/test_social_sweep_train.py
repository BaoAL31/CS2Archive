"""Experimental sweep: leakage guards, chronological grouped holdout, honest refusal."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from cs2archive.shorts.social.config import load_config
from cs2archive.shorts.social.models import LinkRecord, MetricRecord, PostRecord
from cs2archive.shorts.social.store import SocialStore
from cs2archive.shorts.social.train import (
    DatasetRow, _precision_at_k, _spearman, assert_no_leakage,
    evaluate_allstar_gameplay, evaluate_platform, fit_cohort_targets,
    load_allstar_rows, load_platform_rows, run_sweep, split_rows,
)


def _config(tmp_path) -> object:
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "database": str(tmp_path / "db.sqlite3"),
        "raw_root": str(tmp_path / "raw"),
        "platforms": {"youtube": {"enabled": False}},
        "training": {"min_rows_per_platform": 25,
                     "output": str(tmp_path / "train.json")},
    }), encoding="utf-8")
    return load_config(path)


def _seed(tmp_path, *, count=40) -> SocialStore:
    store = SocialStore(tmp_path / "db.sqlite3")
    base = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for i in range(count):
        week = i // 4
        published = (base + timedelta(days=week * 7 + i % 3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        post_id = f"v{i:03d}"
        post = PostRecord(
            platform="youtube", post_id=post_id, url=f"https://x/{post_id}",
            creator_name=f"creator{i % 3}", caption=f"clip {i}",
            published_at=published, duration_seconds=float(20 + i % 30),
            format_label="short", format_confidence=0.98,
            format_evidence={"verified": True, "method": "shorts_route_200_marker"})
        store.upsert_post(post, run_id="seed", raw_id=None, observed_at=published)
        kills = float(i % 5)
        views = int(100 * math.exp(0.02 * kills + 0.01 * (i % 30)) + i)
        store.add_snapshot(post, MetricRecord(views=views, likes=views // 20,
                                             metric_source="test"),
                           observed_at=published, run_id="seed", raw_id=None)
        store.set_feature(platform="youtube", post_id=post_id, feature="kills",
                          value_num=kills, kind="gameplay", provenance="measured",
                          confidence=1.0, run_id="seed")
        store.set_feature(platform="youtube", post_id=post_id, feature="caption_length",
                          value_num=float(len(post.caption)), kind="context",
                          provenance="measured", confidence=1.0, run_id="seed")
        store.set_feature(platform="youtube", post_id=post_id, feature="duration_seconds",
                          value_num=post.duration_seconds, kind="presentation",
                          provenance="measured", confidence=1.0, run_id="seed")
        # Pairs share one verified clip identity -> two posts per group.
        clip = f"clip{i // 2:03d}"
        store.upsert_link(LinkRecord(
            platform="youtube", post_id=post_id, foundation_clip_id=clip,
            match_id=f"m{i // 4}", method="clip_id_in_caption", confidence=1.0,
            ambiguous=False), run_id="seed")
    return store


def test_outcome_metric_as_feature_is_rejected():
    with pytest.raises(ValueError):
        assert_no_leakage(["kills", "views"])
    with pytest.raises(ValueError):
        assert_no_leakage(["like_count"])
    assert_no_leakage(["kills", "duration_seconds", "caption_length"])


def test_load_rows_drops_missing_target_and_outcome_features(tmp_path):
    store = _seed(tmp_path, count=6)
    try:
        post = PostRecord(platform="youtube", post_id="zero",
                          published_at="2026-09-01T00:00:00Z",
                          format_label="short", format_confidence=0.98,
                          format_evidence={"verified": True})
        store.upsert_post(post, run_id="seed", raw_id=None,
                          observed_at="2026-09-01T00:00:00Z")
        store.add_snapshot(post, MetricRecord(views=0, metric_source="test"),
                           observed_at="2026-09-01T00:00:00Z", run_id="seed", raw_id=None)
        # A metric-named feature must never reach the design matrix.
        store.set_feature(platform="youtube", post_id="v000", feature="views",
                          value_num=999999.0, kind="gameplay", provenance="measured",
                          run_id="seed")
        rows, stats = load_platform_rows(store, "youtube")
        assert stats["zero_engagement"] == 1
        assert all("views" not in row.features for row in rows)
        assert stats["kept"] == len(rows)
    finally:
        store.close()


def test_split_purges_groups_that_straddle_the_cut(tmp_path):
    store = _seed(tmp_path, count=39)
    try:
        rows, _ = load_platform_rows(store, "youtube")
        split = split_rows(rows)
        train_groups = {row.group_id for row in split["train"]}
        test_groups = {row.group_id for row in split["test"]}
        assert not (train_groups & test_groups)
        assert split["purged"] > 0  # the seed pairs groups across the cut
        # Chronological: every training row is older than every test row.
        assert max(row.published_at for row in split["train"]) <= \
            min(row.published_at for row in split["test"])
    finally:
        store.close()


def test_too_little_data_refuses_instead_of_scoring(tmp_path):
    store = _seed(tmp_path, count=4)
    try:
        config = _config(tmp_path)
        report = run_sweep(config, experimental=True, min_rows=25)
        assert report["status"] == "refused"
        assert report["fitted_platforms"] == []
        assert "no platform had enough verified rows" in report["reason"]
        for result in report["platforms"].values():
            assert result["status"] == "refused"
            assert "results" not in result or not result["results"]
    finally:
        store.close()


def test_requires_experimental_flag(tmp_path):
    store = _seed(tmp_path, count=40)
    store.close()
    report = run_sweep(_config(tmp_path), experimental=False)
    assert report["status"] == "refused"
    assert "--experimental" in report["reason"]


def test_fitted_sweep_reports_baselines_and_ablations(tmp_path):
    store = _seed(tmp_path, count=60)
    try:
        config = _config(tmp_path)
        report = run_sweep(config, experimental=True, min_rows=20)
        result = report["platforms"]["youtube"]
        assert result["status"] == "fitted", result
        labels = set(result["results"])
        assert "baseline_cohort_median" in labels
        assert "ridge:all" in labels and "hgb:all" in labels
        assert "ridge:no_gameplay" in labels and "hgb:no_context" in labels
        for label, score in result["results"].items():
            assert "error" not in score, (label, score)
            assert score["mae"] >= 0
        assert result["target"]["metric"] == "views"
        assert "24h" in result["target"]["note"]
        assert "causal" in report["constraints"][-1]
        assert (tmp_path / "train.json").exists()
    finally:
        store.close()


def test_other_platforms_cannot_train_without_comparable_metric(tmp_path):
    store = _seed(tmp_path, count=40)
    try:
        result = evaluate_platform([], platform="reddit", min_rows=1)
        assert result["status"] == "refused"
        rows, stats = load_platform_rows(store, "reddit")
        assert rows == [] and stats["target_metric"] is None
    finally:
        store.close()


def test_only_verified_short_format_rows_are_usable(tmp_path):
    store = _seed(tmp_path, count=39)
    try:
        long_post = PostRecord(platform="youtube", post_id="long1",
                               published_at="2026-10-15T00:00:00Z",
                               format_label="long", format_confidence=0.95,
                               format_evidence={"verified": True})
        store.upsert_post(long_post, run_id="seed", raw_id=None,
                          observed_at="2026-10-15T00:00:00Z")
        store.add_snapshot(long_post, MetricRecord(views=999, metric_source="test"),
                           observed_at="2026-10-15T00:00:00Z", run_id="seed", raw_id=None)
        cand = PostRecord(platform="youtube", post_id="cand1",
                          published_at="2026-10-16T00:00:00Z",
                          format_label="candidate_short", format_confidence=0.4,
                          format_evidence={"verified": False})
        store.upsert_post(cand, run_id="seed", raw_id=None,
                          observed_at="2026-10-16T00:00:00Z")
        store.add_snapshot(cand, MetricRecord(views=500, metric_source="test"),
                           observed_at="2026-10-16T00:00:00Z", run_id="seed", raw_id=None)
        rows, stats = load_platform_rows(store, "youtube")
        assert stats["unverified_format"] == 2
        assert {row.post_id for row in rows}.isdisjoint({"long1", "cand1"})
        assert all(row.features.get("format_label") == "short" for row in rows)
    finally:
        store.close()


def test_cohort_targets_are_fitted_on_training_rows_only():
    def row(post_id, cohort, published, raw, target=None):
        return DatasetRow(platform="youtube", post_id=post_id, published_at=published,
                          cohort=cohort, group_id=f"post:{post_id}",
                          group_basis="post_only", target_raw=raw, target=target)

    train = [row(f"t{i}", "2026-W40", f"2026-10-0{i + 1}T00:00:00Z", raw)
             for i, raw in enumerate([100, 200, 400])]
    test = [row("x1", "2026-W44", "2026-10-31T00:00:00Z", 1000),
            row("x2", "2026-W40", "2026-10-29T00:00:00Z", 100)]
    info = fit_cohort_targets(train, test)
    train_targets = [round(item.target, 6) for item in train]
    # A different held-out outcome must not move any training target.
    test2 = [row("x1", "2026-W44", "2026-10-31T00:00:00Z", 999999),
             row("x2", "2026-W40", "2026-10-29T00:00:00Z", 100)]
    fit_cohort_targets(train, test2)
    assert [round(item.target, 6) for item in train] == train_targets
    assert info["test_cohort_fallbacks"] == 1
    assert test[0].cohort_seen is False       # unseen cohort -> train-wide median
    assert test[1].cohort_seen is True        # cohort seen in train
    assert test[0].target == round(math.log1p(1000) - info["global_train_median"], 6)
    assert test[1].target == round(
        math.log1p(100) - math.log1p(200), 6)  # cohort median from TRAIN only
    assert all(item.target is not None for item in train)


def test_groups_unify_clips_of_the_same_match():
    from cs2archive.shorts.social.identity import assign_groups

    links = {
        ("youtube", "p1"): [{"foundation_clip_id": "clipA", "match_id": "m1",
                             "confidence": 1.0, "ambiguous": 0}],
        ("youtube", "p2"): [{"foundation_clip_id": "clipB", "match_id": "m1",
                             "confidence": 1.0, "ambiguous": 0}],
        ("youtube", "p3"): [{"foundation_clip_id": "clipC", "match_id": "m2",
                             "confidence": 1.0, "ambiguous": 0}],
        ("youtube", "p4"): [],
    }
    groups = assign_groups([(p, "p1") for p in ["youtube"]] + [
        ("youtube", "p2"), ("youtube", "p3"), ("youtube", "p4")], links)
    assert groups[("youtube", "p1")]["group_id"] == \
        groups[("youtube", "p2")]["group_id"]
    assert groups[("youtube", "p3")]["group_id"] != groups[("youtube", "p1")]["group_id"]
    assert groups[("youtube", "p4")]["basis"] == "post_only"
    assert groups[("youtube", "p4")]["verified"] is False


def test_low_identity_coverage_refuses_credible_evaluation(tmp_path):
    store = _seed(tmp_path, count=60)
    try:
        # Remove most identity links: only two posts keep a verified clip.
        store.conn.execute("DELETE FROM clip_identity_links WHERE post_id NOT IN "
                           "('v000','v001')")
        rows, stats = load_platform_rows(store, "youtube")
        assert stats["verified_identity_rows"] == 2
        result = evaluate_platform(rows, platform="youtube", min_rows=20)
        assert result["status"] == "refused"
        assert "verified_only identity" in result["reason"]
        assert result["identity"]["excluded_unverified"] == len(rows) - 2
        assert "results" not in result
        # Opting in scores all rows but labels the residual leakage risk.
        restricted = evaluate_platform(rows, platform="youtube", min_rows=20,
                                       allow_unverified_groups=True)
        assert restricted["status"] == "restricted"
        assert restricted["identity"]["mode"] == "unverified_risk"
    finally:
        store.close()


def test_ablation_feature_kinds_are_respected():
    from cs2archive.shorts.social.train import _feature_kinds_map, _features_for

    row = DatasetRow(platform="youtube", post_id="p", published_at="2026-10-01T00:00:00Z",
                     cohort="2026-W40", group_id="g", group_basis="verified",
                     target_raw=10, target=0.0,
                     features={"creator_name": "c", "format_label": "short",
                               "duration_seconds": 20.0, "kills": 3.0,
                               "age_hours_at_observation": 500.0})
    kinds = _feature_kinds_map([row])
    no_context = _features_for(row, ("gameplay", "presentation"), kinds)
    assert "creator_name" not in no_context
    assert set(no_context) == {"format_label", "duration_seconds", "kills"}
    no_presentation = _features_for(row, ("gameplay", "context"), kinds)
    assert "format_label" not in no_presentation
    assert "duration_seconds" not in no_presentation
    assert "creator_name" in no_presentation
    # Measurement age is never a predictor, in any ablation.
    for kinds_set in (("gameplay",), ("presentation",), ("context",),
                      ("gameplay", "presentation", "context")):
        assert "age_hours_at_observation" not in _features_for(row, kinds_set, kinds)


def test_measurement_age_is_rejected_by_the_leakage_guard():
    with pytest.raises(ValueError):
        assert_no_leakage(["kills", "age_hours_at_observation"])


def test_spearman_uses_average_ranks_for_ties():
    assert _spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert _spearman([1, 1, 2], [1, 2, 2]) == pytest.approx(0.5)
    assert _spearman([1, 1, 1], [1, 2, 3]) is None
    try:
        from scipy.stats import spearmanr
    except Exception:  # noqa: BLE001 - scipy is optional for this assertion
        return
    pred = [0.5, 0.5, 1.0, 2.0, 2.0, 3.0]
    actual = [1.0, 2.0, 2.0, 2.0, 3.0, 1.0]
    assert _spearman(pred, actual) == pytest.approx(
        float(spearmanr(pred, actual).statistic), abs=1e-9)


def test_precision_at_k_is_deterministic_under_ties():
    assert _precision_at_k([1, 1, 1], [1, 1, 1], 2) == 1.0
    # Ties break by original index, so the same call always returns the same k.
    first = _precision_at_k([5, 5, 1], [1, 5, 5], 1)
    assert first == _precision_at_k([5, 5, 1], [1, 5, 5], 1)
    assert first == 0.0  # index 0 wins the tie and is not the actual top row


def test_zero_engagement_is_kept_and_modelled_with_log1p(tmp_path):
    store = _seed(tmp_path, count=39)
    try:
        from cs2archive.shorts.social.models import LinkRecord, MetricRecord, PostRecord
        for post_id in ("zeroA", "zeroB"):
            post = PostRecord(
                platform="youtube", post_id=post_id,
                published_at="2026-09-20T00:00:00Z", creator_name="creator0",
                format_label="short", format_confidence=0.98,
                format_evidence={"verified": True})
            store.upsert_post(post, run_id="seed", raw_id=None,
                              observed_at="2026-09-20T00:00:00Z")
            store.add_snapshot(post, MetricRecord(views=0, metric_source="test"),
                               observed_at="2026-09-20T00:00:00Z", run_id="seed",
                               raw_id=None)
            store.set_feature(platform="youtube", post_id=post_id, feature="kills",
                              value_num=1.0, kind="gameplay", provenance="measured",
                              run_id="seed")
            store.upsert_link(LinkRecord(
                platform="youtube", post_id=post_id,
                foundation_clip_id=f"clip-{post_id}", match_id=f"m-{post_id}",
                method="clip_id_in_caption", confidence=1.0, ambiguous=False),
                run_id="seed")
        rows, stats = load_platform_rows(store, "youtube")
        assert stats["zero_engagement"] == 2
        assert {row.post_id for row in rows} >= {"zeroA", "zeroB"}
        assert all(row.target_raw >= 0 for row in rows)
    finally:
        store.close()


def test_missing_values_get_explicit_missing_indicators():
    from cs2archive.shorts.social.train import _feature_universe, _features_for

    def row(post_id, features):
        return DatasetRow(platform="youtube", post_id=post_id,
                          published_at="2026-10-01T00:00:00Z", cohort="2026-W40",
                          group_id=f"post:{post_id}", group_basis="verified",
                          target_raw=10, target=0.0, features=features)

    train = [row("a", {"kills": 2.0, "duration_seconds": 20.0}),
             row("b", {"kills": 3.0})]
    kinds = {"kills": "gameplay", "duration_seconds": "presentation"}
    universe = _feature_universe(train, ("gameplay", "presentation"), kinds)
    features = _features_for(train[1], ("gameplay", "presentation"), kinds, universe)
    # Absent duration is flagged missing instead of reading as an observed zero.
    assert features["duration_seconds__missing"] == 1.0
    assert "duration_seconds" not in features
    assert features["kills"] == 3.0
    assert "kills__missing" not in features


def test_allstar_gameplay_experiment_is_match_held_out_and_dated_honestly(tmp_path):
    store = _seed(tmp_path, count=60)
    try:
        from cs2archive.shorts.social.models import LinkRecord, MetricRecord, PostRecord
        import cs2archive.shorts.social.train as train_module

        def add_allstar(post_id, match_id, views, kills):
            post = PostRecord(platform="allstar", post_id=post_id,
                              caption="clip", creator_name=f"p{post_id[-1]}",
                              format_label="clip", format_confidence=0.4,
                              format_evidence={"verified": False})
            store.upsert_post(post, run_id="seed", raw_id=None,
                              observed_at="2026-10-01T00:00:00Z")
            store.add_snapshot(post, MetricRecord(views=views,
                                                  metric_source="allstar_store"),
                               observed_at="2026-10-01T00:00:00Z", run_id="seed",
                               raw_id=None)
            for feature, value in (("kills", kills),
                                   ("headshots", float(kills // 2)),
                                   ("kill_span_seconds", 3.0),
                                   ("kills_per_10s", 2.0)):
                store.set_feature(platform="allstar", post_id=post_id,
                                  feature=feature, value_num=value, kind="gameplay",
                                  provenance="measured", confidence=1.0, run_id="seed")
            store.set_feature(platform="allstar", post_id=post_id,
                              feature="clip_span_seconds", value_num=8.0,
                              kind="presentation", provenance="measured",
                              confidence=1.0, run_id="seed")
            store.upsert_link(LinkRecord(
                platform="allstar", post_id=post_id, foundation_clip_id=f"c{post_id}",
                match_id=match_id, method="allstar_clip_id", confidence=1.0,
                ambiguous=False), run_id="seed")

        for i in range(120):
            add_allstar(f"a{i:03d}", f"match{i // 4}", views=i * 10, kills=i % 5)
        rows, stats = load_allstar_rows(store)
        assert stats["kept"] >= 100
        result = train_module.evaluate_allstar_gameplay(rows, min_rows=60)
        assert result["status"] == "fitted", result
        assert result["label"]["forecasts_youtube"] is False
        assert result["label"]["publication_dates"] is False
        assert result["split"]["train_matches"] >= 5
        assert "ridge:density_only" in result["results"]
        assert any(row.target is not None for row in rows)
    finally:
        store.close()


def test_allstar_creator_baseline_falls_back_to_training_median(tmp_path):
    """An unseen creator must not predict 0.0 on an absolute log1p scale."""
    from cs2archive.shorts.social.train import _creator_baseline

    def row(post_id, creator, target):
        return DatasetRow(platform="allstar", post_id=post_id, published_at="",
                          cohort="allstar-no-dates", group_id=f"m{post_id}",
                          group_basis="verified", target_raw=1.0, target=target,
                          features={"creator_name": creator} if creator else {})

    train = [row("a", "known", 4.0), row("b", "known", 6.0),
             row("c", "other", 5.0)]
    test = [row("x", "unseen", 5.0), row("y", "known", 6.0)]
    baseline = _creator_baseline(train, test)
    assert baseline["fallback"] == "training-wide median"
    assert baseline["fallback_count"] == 1
    # global median of [4,5,6] = 5.0, and known -> mean 5.0; never 0.0
    assert baseline["predictions"] == [5.0, 5.0]
    # No creator feature anywhere -> the baseline is omitted, not fabricated.
    bare = [row("z", "", 5.0)]
    omitted = _creator_baseline(bare, bare)
    assert omitted["skipped"] == "no creator feature on held-out rows"


def test_allstar_experiment_creator_baseline_is_not_zero(tmp_path):
    store = _seed(tmp_path, count=60)
    try:
        from cs2archive.shorts.social.models import LinkRecord, MetricRecord, PostRecord
        import cs2archive.shorts.social.train as train_module

        def add(post_id, match_id, views, creator):
            post = PostRecord(platform="allstar", post_id=post_id, caption="c",
                              creator_name=creator, format_label="clip",
                              format_confidence=0.4,
                              format_evidence={"verified": False})
            store.upsert_post(post, run_id="seed", raw_id=None,
                              observed_at="2026-10-01T00:00:00Z")
            store.add_snapshot(post, MetricRecord(views=views,
                                                  metric_source="allstar_store"),
                               observed_at="2026-10-01T00:00:00Z", run_id="seed",
                               raw_id=None)
            store.set_feature(platform="allstar", post_id=post_id, feature="kills",
                              value_num=2.0, kind="gameplay", provenance="measured",
                              confidence=1.0, run_id="seed")
            store.set_feature(platform="allstar", post_id=post_id,
                              feature="clip_span_seconds", value_num=8.0,
                              kind="presentation", provenance="measured",
                              confidence=1.0, run_id="seed")
            store.upsert_link(LinkRecord(
                platform="allstar", post_id=post_id,
                foundation_clip_id=f"c{post_id}", match_id=match_id,
                method="allstar_clip_id", confidence=1.0, ambiguous=False),
                run_id="seed")

        for i in range(120):
            add(f"b{i:03d}", f"match{i // 4}", views=100 + i, creator=f"creator{i % 5}")
        rows, _ = load_allstar_rows(store)
        result = train_module.evaluate_allstar_gameplay(rows, min_rows=60)
        baseline = result["results"]["baseline_creator_median"]
        assert "error" not in baseline
        # A 0.0 fallback predicts log1p(1)=0 and would score an absurd MAE.
        assert baseline["mae"] < 5.0, baseline
        assert baseline.get("fallback") in {"training-wide median", None}
    finally:
        store.close()


def test_own_channel_linked_experiment_refuses_without_verified_links(tmp_path):
    store = _seed(tmp_path, count=60)
    try:
        from cs2archive.shorts.social.train import evaluate_own_channel_linked

        rows, _ = load_platform_rows(store, "youtube")
        own = [row for row in rows if row.post_id in {"v000", "v001"}]
        result = evaluate_own_channel_linked(rows, own_channel_id="UCnone",
                                             min_rows=5)
        assert result["status"] == "refused"
        assert "verified demo link" in result["reason"]
        assert result["identity"]["verified_linked"] == 0
        # Opting the own channel in flips the same rows to a real evaluation.
        for row in own:
            row.creator_id = "UCown"
        fitted = evaluate_own_channel_linked(rows, own_channel_id="UCown",
                                            min_rows=1)
        # Past the verified-link gate the only remaining objection is holdout
        # size, which proves the own-channel population was actually evaluated.
        assert fitted["status"] in {"fitted", "refused"}
        assert "verified demo link" not in (fitted.get("reason") or "")
    finally:
        store.close()


def test_analytics_diagnostics_never_become_the_target(tmp_path):
    store = _seed(tmp_path, count=40)
    try:
        from cs2archive.shorts.social.train import analytics_diagnostics

        diag = analytics_diagnostics(store, own_channel_id="UCown", min_rows=5)
        assert diag["status"] == "unavailable"
        assert diag["used_as_target"] is False
        assert "separate family" in diag["note"]
        store.add_analytics_snapshot(
            platform="youtube", post_id="v000", window_start="2026-09-01",
            window_end="2026-09-07", metrics={"impressions": 1000, "ctr": 0.05,
                                              "average_view_percentage": 40.0},
            metric_family="youtube_analytics", fetched_at="2026-11-06T00:00:00Z",
            run_id="r")
        store.conn.execute("UPDATE posts SET creator_id='UCown' WHERE post_id='v000'")
        diag = analytics_diagnostics(store, own_channel_id="UCown", min_rows=1)
        assert diag["status"] == "available"
        assert diag["median_impressions"] == 1000.0
        assert diag["used_as_target"] is False
        # The public target for that post is untouched by the analytics row.
        rows, _ = load_platform_rows(store, "youtube")
        target = {row.post_id: row.target_raw for row in rows if row.post_id == "v000"}
        assert target and target["v000"] < 1000
    finally:
        store.close()
