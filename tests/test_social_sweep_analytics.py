"""Analytics: separate metric family, windowed, append-only, refuses cleanly."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cs2archive.shorts.social import analytics
from cs2archive.shorts.social.models import MetricRecord, PostRecord
from cs2archive.shorts.social.store import SocialStore


def _token(tmp_path, scopes=("https://www.googleapis.com/auth/yt-analytics.readonly",)):
    path = tmp_path / "token_youtube_analytics.json"
    path.write_text(json.dumps({
        "token": "x", "refresh_token": "y", "client_id": "c", "client_secret": "s",
        "scopes": list(scopes)}), encoding="utf-8")
    return path


def _post(store, post_id="own1", published="2026-10-01T00:00:00Z"):
    post = PostRecord(platform="youtube", post_id=post_id,
                      published_at=published, format_label="short",
                      format_confidence=0.98, format_evidence={"verified": True})
    store.upsert_post(post, run_id="r", raw_id=None,
                      observed_at=published)
    store.add_snapshot(post, MetricRecord(views=10, likes=1,
                                          metric_source="youtube_data_api_v3"),
                       observed_at=published, run_id="r", raw_id=None)


class FakeReports:
    def __init__(self, payload):
        self.payload = payload

    def query(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self, num_retries=0):
        return self.payload


class FakeService:
    def __init__(self, payload):
        self._reports = FakeReports(payload)

    def reports(self):
        return self._reports


def test_analytics_status_refuses_without_token(tmp_path):
    status = analytics.analytics_status(token_path=tmp_path / "missing.json")
    assert status["available"] is False
    assert "auth_youtube_analytics" in status["reason"]


def test_analytics_status_refuses_without_scope(tmp_path):
    token = _token(tmp_path, scopes=("https://www.googleapis.com/auth/youtube.readonly",))
    status = analytics.analytics_status(token_path=token)
    assert status["available"] is False
    assert "missing scope" in status["reason"]


def test_analytics_status_accepts_a_scoped_token(tmp_path):
    assert analytics.analytics_status(token_path=_token(tmp_path))["available"] is True


def test_analysis_window_skips_partial_and_too_new(tmp_path):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    # 2026-10-01T00:00Z is still 2026-09-30 in Pacific, so the first full local
    # day after publication is 2026-10-01 (same rule as export_pov_analytics).
    assert analytics.analysis_window("2026-10-01T00:00:00Z", now) == \
        ("2026-10-01", "2026-10-07")
    # Published today: the window is not a complete set of days yet.
    assert analytics.analysis_window("2026-11-06T00:00:00Z", now) is None
    assert analytics.analysis_window(None, now) is None


def test_ingest_refuses_cleanly_when_token_missing(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _post(store)
        stats = analytics.ingest_analytics(
            store, videos=[{"post_id": "own1", "published_at": "2026-10-01T00:00:00Z"}],
            run_id="analytics-1", token_path=tmp_path / "none.json")
        assert stats["available"] is False and stats["written"] == 0
        assert "auth_youtube_analytics" in stats["reason"]
        # The refusal is recorded, and nothing entered either metric family.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0] == 0
        codes = [r["error_code"] for r in store.conn.execute(
            "SELECT error_code FROM collection_errors")]
        assert "analytics_unavailable" in codes
    finally:
        store.close()


def test_ingest_writes_a_separate_windowed_family(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _post(store)
        payload = {"columnHeaders": [{"name": "views"}, {"name": "impressions"},
                                     {"name": "impressionClickThroughRate"},
                                     {"name": "averageViewDuration"},
                                     {"name": "averageViewPercentage"},
                                     {"name": "estimatedMinutesWatched"},
                                     {"name": "subscribersGained"}],
                   "rows": [[123, 4567, 0.034, 21.5, 48.2, 44.1, 2]]}
        stats = analytics.ingest_analytics(
            store, videos=[{"post_id": "own1", "published_at": "2026-10-01T00:00:00Z"}],
            run_id="analytics-1", token_path=_token(tmp_path),
            now=datetime(2026, 11, 6, tzinfo=timezone.utc),
            service=FakeService(payload), log=lambda *_: None)
        assert stats["written"] == 1 and stats["candidates"] == 1
        row = store.conn.execute("SELECT * FROM analytics_snapshots").fetchone()
        assert row["metric_family"] == "youtube_analytics"
        assert (row["window_start"], row["window_end"]) == ("2026-10-01", "2026-10-07")
        assert row["impressions"] == 4567 and row["ctr"] == pytest.approx(0.034)
        assert row["views"] == 123
        assert store.analytics_counts() == {"youtube:youtube_analytics":
                                            {"rows": 1, "posts": 1}}
    finally:
        store.close()


def test_analytics_never_feeds_the_public_views_target(tmp_path):
    """The strict sweep must see only the public snapshot family."""
    from cs2archive.shorts.social.train import load_platform_rows

    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _post(store, post_id="own1", published="2026-10-01T00:00:00Z")
        payload = {"columnHeaders": [{"name": "views"}], "rows": [[999999]]}
        analytics.ingest_analytics(
            store, videos=[{"post_id": "own1", "published_at": "2026-10-01T00:00:00Z"}],
            run_id="analytics-1", token_path=_token(tmp_path),
            now=datetime(2026, 11, 6, tzinfo=timezone.utc),
            service=FakeService(payload), log=lambda *_: None)
        rows, _stats = load_platform_rows(store, "youtube")
        assert len(rows) == 1
        # The target comes from metric_snapshots (10), never analytics (999999).
        assert rows[0].target_raw == 10.0
        assert "impressions" not in rows[0].features
        assert "ctr" not in rows[0].features
    finally:
        store.close()


def test_analytics_snapshots_are_append_only_and_idempotent(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.add_analytics_snapshot(platform="youtube", post_id="p", window_start="a",
                                     window_end="b", metrics={"views": 1},
                                     metric_family="youtube_analytics",
                                     fetched_at="2026-11-06T00:00:00Z", run_id="r")
        assert store.add_analytics_snapshot(
            platform="youtube", post_id="p", window_start="a", window_end="b",
            metrics={"views": 1}, metric_family="youtube_analytics",
            fetched_at="2026-11-06T00:00:00Z", run_id="r") is False  # idempotent
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE analytics_snapshots SET views=5")
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("DELETE FROM analytics_snapshots")
    finally:
        store.close()


def test_empty_analytics_report_is_unavailable_not_zero():
    assert analytics.report_values({"rows": [], "columnHeaders": [
        {"name": "views"}]}) == {}
    values = analytics.report_values({"rows": [[3]], "columnHeaders": [
        {"name": "views"}]})
    assert values == {"views": 3}


def test_dead_credential_fails_fast_with_one_terminal_error(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _post(store, post_id="own1", published="2026-10-01T00:00:00Z")
        _post(store, post_id="own2", published="2026-10-02T00:00:00Z")

        class DeadService:
            def __init__(self):
                self.calls = 0

            def reports(self):
                return self

            def query(self, **kwargs):
                self.calls += 1
                return self

            def execute(self, num_retries=0):
                raise RuntimeError("RefreshError: invalid_grant: Token has been "
                                   "expired or revoked.")

        service = DeadService()
        stats = analytics.ingest_analytics(
            store,
            videos=[{"post_id": "own1", "published_at": "2026-10-01T00:00:00Z"},
                    {"post_id": "own2", "published_at": "2026-10-02T00:00:00Z"}],
            run_id="analytics-dead", token_path=_token(tmp_path),
            now=datetime(2026, 11, 6, tzinfo=timezone.utc),
            service=service, log=lambda *_: None)
        assert service.calls == 1                 # fail fast, not 2 queries
        assert stats["available"] is False
        assert "auth_youtube_analytics" in stats["reason"]
        codes = [r["error_code"] for r in store.conn.execute(
            "SELECT error_code FROM collection_errors")]
        assert codes.count("analytics_auth_failed") == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM analytics_snapshots").fetchone()[0] == 0
    finally:
        store.close()


def test_is_auth_failure_matches_real_google_errors():
    assert analytics.is_auth_failure(
        RuntimeError("RefreshError: invalid_grant: Token has been expired or revoked."))
    assert analytics.is_auth_failure(RuntimeError("HttpError 401 unauthorized"))
    assert not analytics.is_auth_failure(RuntimeError("HttpError 500 backend error"))
