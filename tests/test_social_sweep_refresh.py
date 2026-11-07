"""Bounded refresh: append a later snapshot, never edit history."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cs2archive.shorts.social.config import load_config
from cs2archive.shorts.social.context import Env
from cs2archive.shorts.social.models import MetricRecord, PostRecord
from cs2archive.shorts.social.refresh import refresh_database
from cs2archive.shorts.social.store import SocialStore


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""
        self.headers = {}
        self.content = b""

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, follow_redirects=True):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, dict(params or {}))


def _config(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "database": str(tmp_path / "db.sqlite3"),
        "raw_root": str(tmp_path / "raw"),
        "window": {"start": "2026-08-01T00:00:00Z"},
        "rate_limit": {"min_interval_seconds": 0.0, "max_requests_per_run": 10},
        "platforms": {
            "youtube": {"enabled": True, "mode": "api"},
            "instagram": {"enabled": True, "mode": "graph_business_discovery",
                          "creators": ["cs2povarchive"], "max_pages": 1},
            "reddit": {"enabled": True, "communities": ["x"]},
            "allstar": {"enabled": True, "mode": "local_store_import"},
            "tiktok": {"enabled": True, "mode": "oembed"},
        },
    }), encoding="utf-8")
    return load_config(path)


def _seed(store):
    post = PostRecord(platform="youtube", post_id="vid1", url="https://x",
                      creator_name="chan", caption="t",
                      published_at="2026-09-01T00:00:00Z",
                      format_label="short", format_confidence=0.98,
                      format_evidence={"verified": True})
    store.upsert_post(post, run_id="old", raw_id=None,
                      observed_at="2026-09-02T00:00:00Z")
    store.add_snapshot(post, MetricRecord(views=10, likes=1,
                                         metric_source="youtube_data_api_v3"),
                       observed_at="2026-09-02T00:00:00Z", run_id="old", raw_id=None)


def test_refresh_appends_a_later_snapshot(tmp_path):
    config = _config(tmp_path)
    store = SocialStore(config.database)
    _seed(store)
    store.close()

    payload = {"items": [{
        "id": "vid1",
        "snippet": {"publishedAt": "2026-09-01T00:00:00Z", "channelId": "UC1",
                    "channelTitle": "chan", "title": "t"},
        "contentDetails": {"duration": "PT24S"},
        "statistics": {"viewCount": "99", "likeCount": "5", "commentCount": "2"},
    }]}
    client = FakeClient(lambda url, params: FakeResponse(payload))
    result = refresh_database(config, env=Env(youtube_api_key="k"), client=client,
                              platforms=["youtube"],
                              clock=lambda: datetime(2026, 11, 6, tzinfo=timezone.utc),
                              min_age_hours=1.0)
    run = result["runs"][0]
    assert run["status"] == "ok" and run["snapshots_written"] == 1
    store = SocialStore(config.database)
    try:
        rows = list(store.conn.execute(
            "SELECT observed_at, views FROM metric_snapshots ORDER BY observed_at"))
        assert len(rows) == 2
        assert rows[0]["views"] == 10 and rows[1]["views"] == 99
        assert rows[1]["observed_at"].startswith("2026-11-06")
        assert store.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    finally:
        store.close()


def test_refresh_respects_min_age_and_unavailable_platforms(tmp_path):
    config = _config(tmp_path)
    store = SocialStore(config.database)
    _seed(store)
    store.close()
    client = FakeClient(lambda url, params: FakeResponse({"items": []}))
    result = refresh_database(config, env=Env(youtube_api_key="k"), client=client,
                              platforms=["youtube", "reddit", "allstar"],
                              clock=lambda: datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
                              min_age_hours=1e9)
    runs = {run["platform"]: run for run in result["runs"]}
    assert runs["youtube"]["status"] == "ok"
    assert "older than" in runs["youtube"]["reason"]
    assert runs["reddit"]["status"] == "unavailable"
    assert runs["allstar"]["status"] == "unavailable"
    store = SocialStore(config.database)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM metric_snapshots").fetchone()[0] == 1
    finally:
        store.close()


def test_refresh_is_idempotent_at_the_same_observation_time(tmp_path):
    config = _config(tmp_path)
    store = SocialStore(config.database)
    _seed(store)
    store.close()
    payload = {"items": [{"id": "vid1",
                          "snippet": {"publishedAt": "2026-09-01T00:00:00Z"},
                          "contentDetails": {"duration": "PT24S"},
                          "statistics": {"viewCount": "50"}}]}
    clock = lambda: datetime(2026, 11, 7, tzinfo=timezone.utc)  # noqa: E731
    for _ in range(2):
        refresh_database(config, env=Env(youtube_api_key="k"),
                         client=FakeClient(lambda url, params: FakeResponse(payload)),
                         platforms=["youtube"], clock=clock)
    store = SocialStore(config.database)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM metric_snapshots").fetchone()[0] == 2
    finally:
        store.close()
