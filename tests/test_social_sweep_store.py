"""Store-level guarantees: immutability, NULL metrics, repeat-safe ingestion, secrets."""

from __future__ import annotations

import sqlite3

import pytest

from cs2archive.shorts.social.models import LinkRecord, MetricRecord, PostRecord
from cs2archive.shorts.social.store import SocialStore


def _post(post_id: str = "p1") -> PostRecord:
    return PostRecord(platform="youtube", post_id=post_id, url="https://x/1",
                      creator_name="creator", caption="ACE", published_at="2026-10-01T00:00:00Z")


def _snapshot(store: SocialStore, post_id: str = "p1", observed_at: str = "2026-11-01T00:00:00Z",
              metrics: MetricRecord | None = None) -> bool:
    return store.add_snapshot(
        _post(post_id), metrics or MetricRecord(views=10, likes=1, metric_source="test"),
        observed_at=observed_at, run_id="run-1", raw_id=None)


def test_metric_snapshots_are_append_only(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_post(_post(), run_id="run-1", raw_id=None,
                          observed_at="2026-11-01T00:00:00Z")
        _snapshot(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE metric_snapshots SET views = 999")
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("DELETE FROM metric_snapshots")
        assert store.conn.execute("SELECT views FROM metric_snapshots").fetchone()[0] == 10
    finally:
        store.close()


def test_missing_metrics_are_null_not_zero(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_post(_post(), run_id="run-1", raw_id=None,
                          observed_at="2026-11-01T00:00:00Z")
        _snapshot(store, metrics=MetricRecord(views=5, metric_source="test"))
        row = store.conn.execute("SELECT * FROM metric_snapshots").fetchone()
        assert row["views"] == 5
        for name in ("plays", "likes", "comments", "shares", "saves", "followers"):
            assert row[name] is None, name
    finally:
        store.close()


def test_repeat_ingest_of_same_observation_is_idempotent(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_post(_post(), run_id="run-1", raw_id=None,
                          observed_at="2026-11-01T00:00:00Z")
        assert _snapshot(store) is True
        assert _snapshot(store) is False  # same post + observed_at + source
        assert store.conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0] == 1
    finally:
        store.close()


def test_post_upsert_never_nulls_known_fields(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_post(_post(), run_id="run-1", raw_id=None,
                          observed_at="2026-11-01T00:00:00Z")
        blank = PostRecord(platform="youtube", post_id="p1", url=None, creator_name=None,
                           caption=None)
        store.upsert_post(blank, run_id="run-2", raw_id=None,
                          observed_at="2026-11-02T00:00:00Z")
        row = store.conn.execute("SELECT * FROM posts").fetchone()
        assert row["caption"] == "ACE"
        assert row["creator_name"] == "creator"
        assert row["first_observed_at"] == "2026-11-01T00:00:00Z"
        assert row["last_observed_at"] == "2026-11-02T00:00:00Z"
        # A later observation with a real value fills a previously missing field.
        store.upsert_post(PostRecord(platform="youtube", post_id="p1", url="https://x/1",
                                     duration_seconds=24.0),
                          run_id="run-3", raw_id=None,
                          observed_at="2026-11-03T00:00:00Z")
        assert store.conn.execute(
            "SELECT duration_seconds FROM posts").fetchone()[0] == 24.0
    finally:
        store.close()


def test_credentials_never_reach_disk_or_db(tmp_path):
    secret = "AIzaSySECRETVALUE1234567890"
    store = SocialStore(tmp_path / "db.sqlite3", secrets=(secret,))
    try:
        ref = store.write_raw(
            platform="youtube", run_id="run-1", name="videos",
            payload={"key_echo": secret, "note": f"https://x?key={secret}"},
            endpoint="https://www.googleapis.com/youtube/v3/videos",
            http_status=200, fetched_at="2026-11-01T00:00:00Z",
            params_note=store.scrub_params({"key": secret, "id": "abc"}),
            raw_root=tmp_path / "raw")
        blob = open(ref.content_path, "rb").read()
        assert secret.encode() not in blob
        db_bytes = (tmp_path / "db.sqlite3").read_bytes()
        assert secret.encode() not in db_bytes
        wal = tmp_path / "db.sqlite3-wal"
        if wal.exists():
            assert secret.encode() not in wal.read_bytes()
        params_note = store.conn.execute(
            "SELECT params_note FROM raw_responses").fetchone()[0]
        assert secret not in params_note
        assert "key=[REDACTED]" in params_note
        assert "id=abc" in params_note
    finally:
        store.close()


def test_run_mode_and_initial_status_round_trip(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.start_run(run_id="refresh-x", platform="youtube", mode="refresh")
        row = store.conn.execute("SELECT * FROM collection_runs").fetchone()
        assert row["mode"] == "refresh"
        assert row["status"] == "collecting"
        store.start_run(run_id="collect-y", platform="youtube", mode="api")
        modes = {r["run_id"]: r["mode"] for r in store.conn.execute(
            "SELECT run_id, mode FROM collection_runs")}
        assert modes == {"refresh-x": "refresh", "collect-y": "api"}
    finally:
        store.close()


def test_run_ids_are_disambiguated(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        first = store.unique_run_id("youtube-20261101T000000-abcdef")
        store.start_run(run_id=first, platform="youtube", mode="api")
        second = store.unique_run_id("youtube-20261101T000000-abcdef")
        assert second != first
        assert second.endswith("-r2")
    finally:
        store.close()


def test_links_keep_ambiguity_and_confidence(tmp_path):
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_post(_post(), run_id="run-1", raw_id=None,
                          observed_at="2026-11-01T00:00:00Z")
        store.upsert_link(LinkRecord(
            platform="youtube", post_id="p1", foundation_clip_id="c1", match_id="m1",
            method="clip_id_in_caption", confidence=0.95, ambiguous=True), run_id="run-1")
        row = store.conn.execute("SELECT * FROM clip_identity_links").fetchone()
        assert row["ambiguous"] == 1
        assert store.links_with_confidence() == []
    finally:
        store.close()
