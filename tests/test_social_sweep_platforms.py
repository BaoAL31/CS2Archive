"""Platform adapters: real parsing, NULL metrics, format evidence, retry cap."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cs2archive.shorts.social.collect import collect_platform, env_from_settings  # noqa: F401
from cs2archive.shorts.social.config import load_config
from cs2archive.shorts.social.context import Env
from cs2archive.shorts.social.platforms import allstar, instagram, reddit, tiktok, youtube
from cs2archive.shorts.social.store import SocialStore

FIXED = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.content = text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, follow_redirects=True):
        self.calls.append((url, dict(params or {}), follow_redirects))
        return self.handler(url, dict(params or {}), follow_redirects)


def make_config(tmp_path, platforms: dict, *, window_start="2026-08-01T00:00:00Z",
                max_requests=50, min_interval=0.0, extra=None) -> object:
    data = {
        "database": str(tmp_path / "db.sqlite3"),
        "raw_root": str(tmp_path / "raw"),
        "window": {"start": window_start},
        "rate_limit": {"min_interval_seconds": min_interval,
                       "max_requests_per_run": max_requests},
        "retry": {"max_attempts": 3},
        "platforms": platforms,
        "training": {"min_rows_per_platform": 25,
                     "output": str(tmp_path / "train.json")},
    }
    data.update(extra or {})
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_config(path)


def _channel_payload() -> dict:
    return {"items": [{
        "id": "UC123", "snippet": {"title": "chan"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
        "statistics": {"subscriberCount": "157", "hiddenSubscriberCount": False}}]}


def _video(video_id: str, published: str, seconds: int, views: str) -> dict:
    return {
        "id": video_id,
        "snippet": {"publishedAt": published, "channelId": "UC123",
                    "channelTitle": "chan", "title": f"title {video_id}"},
        "contentDetails": {"duration": f"PT{seconds}S", "definition": "hd"},
        "statistics": {"viewCount": views, "likeCount": "3", "commentCount": "1"},
    }


def test_youtube_collects_ordinary_and_short_with_route_evidence(tmp_path):
    videos = {
        "vidshort": _video("vidshort", "2026-10-31T07:00:22Z", 24, "52"),
        "vidlong": _video("vidlong", "2026-10-30T07:00:22Z", 600, "2000"),
    }

    def handler(url, params, follow):
        if url.endswith("/channels"):
            return FakeResponse(200, _channel_payload())
        if url.endswith("/playlistItems"):
            return FakeResponse(200, {"items": [
                {"contentDetails": {"videoId": vid},
                 "snippet": {"publishedAt": videos[vid]["snippet"]["publishedAt"],
                             "title": vid}} for vid in videos],
                "nextPageToken": None})
        if url.endswith("/videos"):
            ids = str(params.get("id", "")).split(",")
            return FakeResponse(200, {"items": [videos[i] for i in ids if i in videos]})
        if "/shorts/vidshort" in url:
            return FakeResponse(
                200, None,
                text='<link rel="canonical" href="https://www.youtube.com/shorts/vidshort">')
        if "/shorts/vidlong" in url:
            return FakeResponse(
                303, None,
                headers={"location": "https://www.youtube.com/watch?v=vidlong"})
        raise AssertionError(f"unexpected url {url}")

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["@chan"], "max_pages": 1,
        "max_posts": 10}})
    env = Env(youtube_api_key="AIzaTESTKEY0001")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "ok", summary
        assert summary["posts_kept"] == 2
        rows = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM posts")}
        assert rows["vidshort"]["format_label"] == "short"
        assert rows["vidshort"]["format_confidence"] >= 0.9
        assert rows["vidshort"]["duration_seconds"] == 24.0
        assert rows["vidshort"]["published_at"] == "2026-10-31T07:00:22Z"
        assert rows["vidlong"]["format_label"] == "long"
        evidence = json.loads(rows["vidshort"]["format_evidence"])
        assert evidence["verified"] is True
        assert evidence["shorts_probe"]["method"] == "shorts_route_200_marker"
        snap = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM metric_snapshots")}
        assert snap["vidshort"]["views"] == 52
        assert snap["vidshort"]["likes"] == 3
        assert snap["vidshort"]["plays"] is None
        assert snap["vidshort"]["shares"] is None
        assert snap["vidshort"]["saves"] is None
        assert snap["vidshort"]["followers"] == 157
        assert "plays" in json.loads(snap["vidshort"]["unavailable"])
        assert snap["vidshort"]["observed_at"] == "2026-11-05T12:00:00Z"
        # The API key must not survive anywhere on disk.
        assert b"AIzaTESTKEY0001" not in (tmp_path / "db.sqlite3").read_bytes()
        for path in (tmp_path / "raw").rglob("*.json"):
            assert b"AIzaTESTKEY0001" not in path.read_bytes()
    finally:
        store.close()


def test_youtube_duration_alone_is_not_shorts_proof(tmp_path):
    video = _video("v20", "2026-10-31T07:00:22Z", 20, "10")

    def handler(url, params, follow):
        if url.endswith("/videos"):
            return FakeResponse(200, {"items": [video]})
        if "/shorts/" in url:
            return FakeResponse(404, None)
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "video_ids": ["v20"], "max_posts": 5}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        collect_platform(config, store, "youtube", env=env,
                         client=FakeClient(handler), clock=lambda: FIXED)
        row = store.conn.execute("SELECT * FROM posts").fetchone()
        assert row["format_label"] == "candidate_short"
        assert row["format_confidence"] < 0.5
        evidence = json.loads(row["format_evidence"])
        assert evidence["verified"] is False
        assert "not verified" in evidence["note"]
    finally:
        store.close()


def test_instagram_respects_the_post_cap_and_skips_bad_handles(tmp_path):
    def handler(url, params, follow):
        fields = str(params.get("fields") or "")
        if "badhandle" in fields:
            return FakeResponse(400, {"error": {"message": "Cannot find user"}})
        media = [{"id": f"m{i}", "media_type": "VIDEO",
                  "permalink": f"https://www.instagram.com/reel/m{i}/",
                  "timestamp": "2026-11-04T07:08:19+0000",
                  "like_count": i, "comments_count": 0, "caption": "c"}
                 for i in range(30)]
        return FakeResponse(200, {"business_discovery": {
            "followers_count": 10, "media_count": 30,
            "media": {"data": media, "paging": {"cursors": {"after": None}}}}})

    config = make_config(tmp_path, {"instagram": {
        "enabled": True, "mode": "graph_business_discovery",
        "creators": ["badhandle", "good"], "max_pages": 1, "max_posts": 5}})
    env = Env(meta_system_user_token="T", meta_ig_user_id="1")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "instagram", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "partial", summary
        assert summary["posts_kept"] == 5, summary  # cap honoured, overshoot-free
        assert store.conn.execute(
            "SELECT COUNT(*) FROM posts").fetchone()[0] == 5
        errors = [dict(r) for r in store.conn.execute(
            "SELECT error_code, message FROM collection_errors")]
        assert any(e["error_code"] == "creator_unavailable" for e in errors)
        assert any("Cannot find user" in (e["message"] or "") for e in errors)
        notes = json.loads(store.conn.execute(
            "SELECT notes FROM collection_runs").fetchone()[0])
        assert notes["resolved_creators"] == ["good"]
    finally:
        store.close()


def test_instagram_business_discovery_shape(tmp_path):
    payload = {"business_discovery": {
        "followers_count": 908, "media_count": 156,
        "media": {"data": [
            {"id": "179", "media_type": "VIDEO",
             "permalink": "https://www.instagram.com/reel/DfT3zGAkmDM/",
             "timestamp": "2026-11-04T07:08:19+0000",
             "like_count": 6, "comments_count": 0, "caption": "Magixx utility"},
            {"id": "180", "media_type": "IMAGE",
             "permalink": "https://www.instagram.com/p/ABC/",
             "timestamp": "2026-11-01T07:08:19+0000",
             "like_count": 2, "comments_count": 1, "caption": "still"},
        ], "paging": {"cursors": {"after": None}}}}}

    config = make_config(tmp_path, {"instagram": {
        "enabled": True, "mode": "graph_business_discovery",
        "creators": ["cs2povarchive"], "max_pages": 1, "max_posts": 10}})
    env = Env(meta_system_user_token="TOKENSECRET123", meta_ig_user_id="178414")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "instagram", env=env,
                                   client=FakeClient(lambda u, p, f: FakeResponse(200, payload)),
                                   clock=lambda: FIXED)
        assert summary["status"] == "ok"
        rows = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM posts")}
        assert rows["179"]["format_label"] == "short"
        assert rows["179"]["duration_seconds"] is None
        assert rows["180"]["format_label"] == "unknown"
        snap = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM metric_snapshots")}
        assert snap["179"]["likes"] == 6
        assert snap["179"]["views"] is None
        assert snap["179"]["plays"] is None
        assert snap["179"]["followers"] == 908
        assert "plays" in json.loads(snap["179"]["unavailable"])
        assert b"TOKENSECRET123" not in (tmp_path / "db.sqlite3").read_bytes()
    finally:
        store.close()


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_aaa111</id><published>2026-11-05T11:23:37+00:00</published>
    <title>Clean 4k on pistol</title>
    <author><name>someone</name></author>
    <link href="https://www.reddit.com/r/GlobalOffensive/comments/aaa111/x/"/>
    <content type="html">&lt;a href="https://youtube.com/shorts/xyz789"&gt;[link]&lt;/a&gt;</content>
  </entry>
  <entry>
    <id>t3_bbb222</id><published>2026-11-05T10:00:00+00:00</published>
    <title>Long form analysis</title>
    <author><name>other</name></author>
    <link href="https://www.reddit.com/r/GlobalOffensive/comments/bbb222/y/"/>
    <content type="html">&lt;a href="https://www.youtube.com/watch?v=abc"&gt;[link]&lt;/a&gt;</content>
  </entry>
</feed>
"""


def test_reddit_rss_keeps_ordinary_posts_and_nulls_metrics(tmp_path):
    config = make_config(tmp_path, {"reddit": {
        "enabled": True, "mode": "rss", "communities": ["GlobalOffensive"],
        "max_pages": 1, "max_posts": 10}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "reddit", env=env,
                                   client=FakeClient(lambda u, p, f: FakeResponse(200, None, RSS)),
                                   clock=lambda: FIXED)
        assert summary["status"] == "ok"
        rows = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM posts")}
        assert set(rows) == {"aaa111", "bbb222"}
        assert rows["aaa111"]["format_label"] == "short"
        assert rows["bbb222"]["format_label"] == "long"
        assert rows["aaa111"]["url"].endswith("/x/")
        snap = store.conn.execute(
            "SELECT * FROM metric_snapshots WHERE post_id='aaa111'").fetchone()
        for name in ("views", "plays", "likes", "comments", "shares", "saves", "followers"):
            assert snap[name] is None, name
        assert "views" in json.loads(snap["unavailable"])
    finally:
        store.close()


def test_tiktok_oembed_has_no_metrics(tmp_path):
    payload = {"version": "1.0", "type": "video", "title": "clip",
               "author_name": "chan", "author_unique_id": "chan1",
               "width": "325", "height": "578"}
    config = make_config(tmp_path, {"tiktok": {
        "enabled": True, "mode": "oembed",
        "video_urls": ["https://www.tiktok.com/@chan1/video/7435802083655097606"],
        "max_posts": 5}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "tiktok", env=env,
                                   client=FakeClient(lambda u, p, f: FakeResponse(200, payload)),
                                   clock=lambda: FIXED)
        assert summary["status"] == "ok"
        row = store.conn.execute("SELECT * FROM posts").fetchone()
        assert row["post_id"] == "7435802083655097606"
        assert row["format_label"] == "short"
        assert row["published_at"] is None
        snap = store.conn.execute("SELECT * FROM metric_snapshots").fetchone()
        assert snap["views"] is None and snap["likes"] is None
    finally:
        store.close()


def test_allstar_import_respects_unknown_publication_time(tmp_path):
    store_file = tmp_path / "allstar.jsonl"
    store_file.write_text("\n".join(json.dumps(row) for row in [
        {"match_id": "1", "scraped_at": "2026-10-02T02:00:00+00:00",
         "clips": [{"clip_id": "c1", "player": "p", "steamid": "1", "views": 100,
                    "title": "ACE", "published_at": "2026-10-02T02:00:00+00:00",
                    "observed_at": "2026-10-02T02:00:00+00:00"}]},
        {"match_id": "2", "scraped_at": "2026-10-03T02:00:00+00:00",
         "clips": [{"clip_id": "c2", "player": "q", "steamid": "2", "views": 3,
                    "title": "bad clip", "published_at": "2026-10-03T02:00:00+00:00",
                    "observed_at": "2026-10-03T02:00:00+00:00"}]},
    ]) + "\n", encoding="utf-8")
    config = make_config(tmp_path, {"allstar": {
        "enabled": True, "mode": "local_store_import", "store": str(store_file),
        "max_posts": 10}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "allstar", env=env,
                                   clock=lambda: FIXED)
        assert summary["status"] == "ok"
        rows = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM posts")}
        assert rows["c1"]["published_at"] is None  # provenance flag absent
        assert rows["c1"]["format_label"] == "clip"
        snap = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM metric_snapshots")}
        assert snap["c1"]["views"] == 100
        assert snap["c1"]["likes"] is None
        assert snap["c2"]["views"] == 3  # ordinary/poor clips are kept, not filtered
        # Each clip keeps a raw-response pointer to its preserved source row.
        for post_id in ("c1", "c2"):
            raw_id = snap[post_id]["raw_response_id"]
            assert raw_id is not None
            row = store.conn.execute(
                "SELECT name, content_path FROM raw_responses WHERE raw_id=?",
                (raw_id,)).fetchone()
            assert row["name"].startswith("store_row_")
            assert row["content_path"].endswith(".json")
        # Re-imported observations keep provenance through the additive side table.
        links = store.conn.execute(
            "SELECT COUNT(*) FROM snapshot_provenance p JOIN metric_snapshots s "
            "ON s.snapshot_id = p.snapshot_id").fetchone()[0]
        assert links == 2
    finally:
        store.close()


def test_retry_stops_after_three_logged_attempts(tmp_path):
    calls = {"n": 0}

    def handler(url, params, follow):
        calls["n"] += 1
        return FakeResponse(500, None, "boom")

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["@chan"], "max_posts": 5}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "failed"
        assert calls["n"] == 3
        errors = list(store.conn.execute("SELECT * FROM collection_errors ORDER BY error_id"))
        attempts = [e["attempt"] for e in errors if not e["terminal"]]
        assert attempts == [1, 2, 3]
        assert sum(1 for e in errors if e["terminal"]) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        run = store.conn.execute("SELECT * FROM collection_runs").fetchone()
        assert run["status"] == "failed"
    finally:
        store.close()


def test_declared_statuses_never_claim_fake_coverage(tmp_path):
    env = Env()
    config = make_config(tmp_path, {"youtube": {"enabled": True, "mode": "api"},
                                    "tiktok": {"enabled": True, "mode": "oembed"},
                                    "allstar": {"enabled": True, "mode": "local_store_import"}})
    assert youtube.platform_status(config.platform("youtube"), env).status == "unavailable"
    assert tiktok.platform_status(config.platform("tiktok"), env).status == "blocked"
    assert allstar.platform_status(config.platform("allstar"), env).status == "blocked"
    assert instagram.platform_status(config.platform("instagram"), env).status == "unavailable"
    assert reddit.platform_status(config.platform("reddit"), env).status == "unavailable"


def test_disabled_platform_records_unavailable_run(tmp_path):
    config = make_config(tmp_path, {"reddit": {"enabled": False, "communities": ["x"]}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "reddit", env=env, clock=lambda: FIXED)
        assert summary["status"] == "unavailable"
        run = store.conn.execute("SELECT * FROM collection_runs").fetchone()
        assert run["status"] == "unavailable"
        assert run["status_reason"] == "platform disabled in sweep config"
    finally:
        store.close()


@pytest.mark.parametrize("platform", ["youtube", "instagram", "tiktok", "reddit", "allstar"])
def test_every_platform_has_explicit_requirements(tmp_path, platform):
    from cs2archive.shorts.social.platforms import ADAPTERS

    config = make_config(tmp_path, {platform: {"enabled": True}})
    status = ADAPTERS[platform].platform_status(config.platform(platform), Env())
    assert status.status in {"supported", "partial", "blocked", "unavailable"}
    assert status.requirements, platform


def test_authorized_export_import_is_provenanced_and_separated(tmp_path):
    export = tmp_path / "tiktok_export.json"
    export.write_text(json.dumps({
        "platform": "tiktok",
        "posts": [
            {"post_id": "7001", "url": "https://www.tiktok.com/@c/video/7001",
             "creator_name": "c", "caption": "ace", "published_at": "2026-10-01T00:00:00Z",
             "duration_seconds": 21, "observed_at": "2026-11-01T00:00:00Z",
             "metrics": {"views": 500, "likes": 12, "plays": None,
                         "comments": 2, "shares": 1, "saves": 3, "followers": 900}},
            {"post_id": "7002", "url": "https://www.tiktok.com/@c/video/7002",
             "caption": "poor clip", "published_at": "2026-10-02T00:00:00Z",
             "duration_seconds": 9,
             "metrics": {"views": 3}},
        ],
    }), encoding="utf-8")
    config = make_config(tmp_path, {"tiktok": {
        "enabled": True, "mode": "authorized_export",
        "export_paths": [str(export)], "max_posts": 10}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())

    def no_network(*_args, **_kwargs):
        raise AssertionError("authorized export import must not touch the network")

    try:
        summary = collect_platform(config, store, "tiktok", env=env,
                                   client=FakeClient(no_network), clock=lambda: FIXED)
        assert summary["status"] == "ok"
        snap = {r["post_id"]: r for r in store.conn.execute("SELECT * FROM metric_snapshots")}
        assert snap["7001"]["views"] == 500
        assert snap["7001"]["plays"] is None
        assert snap["7001"]["shares"] == 1
        assert snap["7002"]["likes"] is None  # missing stays NULL, not 0
        assert snap["7001"]["metric_source"] == "authorized_export"
        imported = store.conn.execute("SELECT * FROM authorized_imports").fetchone()
        assert imported["rows_read"] == 2 and imported["rows_kept"] == 2
        assert len(imported["sha256"]) == 64
        # Feature extraction is a separate enrich pass, never implicit in collect.
        assert store.conn.execute("SELECT COUNT(*) FROM post_features").fetchone()[0] == 0
    finally:
        store.close()


def test_youtube_200_without_shorts_marker_is_unknown(tmp_path):
    def handler(url, params, follow):
        if url.endswith("/videos"):
            return FakeResponse(200, {"items": [_video("v9", "2026-10-31T00:00:00Z", 20, "5")]})
        if "/shorts/v9" in url:
            return FakeResponse(200, None, text="<html>Before you continue to YouTube</html>")
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "video_ids": ["v9"], "max_posts": 5}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        collect_platform(config, store, "youtube", env=env,
                         client=FakeClient(handler), clock=lambda: FIXED)
        row = store.conn.execute("SELECT * FROM posts").fetchone()
        evidence = json.loads(row["format_evidence"])
        assert evidence["verified"] is False
        assert evidence["shorts_probe"]["verdict"] == "unknown"
        assert row["format_label"] in {"unknown", "candidate_short"}
        if row["format_label"] == "short":  # pragma: no cover - guard
            raise AssertionError("unmarked 200 must never be verified Shorts")
    finally:
        store.close()


def test_youtube_unrelated_redirect_is_unknown(tmp_path):
    def handler(url, params, follow):
        if url.endswith("/videos"):
            return FakeResponse(200, {"items": [_video("v10", "2026-10-31T00:00:00Z", 30, "5")]})
        if "/shorts/v10" in url:
            return FakeResponse(302, None, headers={
                "location": "https://consent.youtube.com/m?continue=x"})
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "video_ids": ["v10"], "max_posts": 5}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        collect_platform(config, store, "youtube", env=env,
                         client=FakeClient(handler), clock=lambda: FIXED)
        evidence = json.loads(
            store.conn.execute("SELECT format_evidence FROM posts").fetchone()[0])
        assert evidence["verified"] is False
        assert evidence["shorts_probe"]["verdict"] == "unknown"
    finally:
        store.close()


def test_posts_and_snapshots_reference_their_raw_response(tmp_path):
    videos = {"vidshort": _video("vidshort", "2026-10-31T07:00:22Z", 24, "52"),
              "vidlong": _video("vidlong", "2026-10-30T07:00:22Z", 600, "2000")}

    def handler(url, params, follow):
        if url.endswith("/channels"):
            return FakeResponse(200, _channel_payload())
        if url.endswith("/playlistItems"):
            return FakeResponse(200, {"items": [
                {"contentDetails": {"videoId": vid},
                 "snippet": {"publishedAt": videos[vid]["snippet"]["publishedAt"],
                             "title": vid}} for vid in videos]})
        if url.endswith("/videos"):
            return FakeResponse(200, {"items": [
                videos[i] for i in str(params.get("id", "")).split(",") if i in videos]})
        if "/shorts/vidshort" in url:
            return FakeResponse(200, None, text='<link rel="canonical" '
                                                'href="https://www.youtube.com/shorts/vidshort">')
        if "/shorts/vidlong" in url:
            return FakeResponse(303, None, headers={
                "location": "https://www.youtube.com/watch?v=vidlong"})
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["@chan"], "max_pages": 1,
        "max_posts": 10}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        run_id = summary["run_id"]
        rows = list(store.conn.execute(
            "SELECT s.post_id, s.raw_response_id, r.run_id, r.name "
            "FROM metric_snapshots s JOIN raw_responses r "
            "ON r.raw_id = s.raw_response_id WHERE s.platform='youtube'"))
        assert len(rows) == 2
        for row in rows:
            assert row["raw_response_id"] is not None
            assert row["run_id"] == run_id
            assert row["name"].startswith(("videos_", "uploads_"))
    finally:
        store.close()


def test_budget_exhaustion_keeps_already_fetched_posts(tmp_path):
    videos = {f"v{i}": _video(f"v{i}", "2026-10-31T07:00:22Z", 24, str(i))
              for i in range(8)}

    def handler(url, params, follow):
        if url.endswith("/channels"):
            return FakeResponse(200, _channel_payload())
        if url.endswith("/playlistItems"):
            return FakeResponse(200, {"items": [
                {"contentDetails": {"videoId": vid},
                 "snippet": {"publishedAt": "2026-10-31T07:00:22Z"}} for vid in videos]})
        if url.endswith("/videos"):
            return FakeResponse(200, {"items": list(videos.values())})
        if "/shorts/" in url:
            return FakeResponse(200, None, text='<link rel="canonical" '
                                                f'href="{url}">')
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["@chan"], "max_pages": 1,
        "max_posts": 8}}, max_requests=4, min_interval=0.0)
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "partial", summary
        assert summary["posts_kept"] > 0
        assert store.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == \
            summary["posts_kept"]
        run = store.conn.execute("SELECT * FROM collection_runs").fetchone()
        assert run["status"] == "partial"
        assert "budget" in (run["notes"] or "").lower()
    finally:
        store.close()


def test_allstar_reimport_never_invents_a_new_observation_time(tmp_path):
    store_file = tmp_path / "allstar.jsonl"
    store_file.write_text(json.dumps({
        "match_id": "9", "scraped_at": "2026-10-02T02:00:00+00:00",
        "clips": [{"clip_id": "cx", "player": "p", "steamid": "1", "views": 42,
                   "title": "clip with no observed_at"}]}) + "\n", encoding="utf-8")
    config = make_config(tmp_path, {"allstar": {
        "enabled": True, "mode": "local_store_import", "store": str(store_file),
        "max_posts": 5}})
    env = Env()
    store = SocialStore(config.database, secrets=env.secrets())

    def run(clock):
        return collect_platform(config, store, "allstar", env=env, clock=clock)

    try:
        first = run(lambda: datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc))
        second = run(lambda: datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc))
        assert first["posts_kept"] == 1 and second["posts_kept"] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM metric_snapshots").fetchone()[0] == 1
        observed = store.conn.execute(
            "SELECT observed_at FROM metric_snapshots").fetchone()[0]
        assert observed.startswith("2026-10-02")  # scraped_at, never "now"
    finally:
        store.close()


def _discovery_handler(channels, titles_by_channel):
    def handler(url, params, follow):
        if url.endswith("/search"):
            return FakeResponse(200, {"items": [
                {"id": {"channelId": cid}, "snippet": {"title": f"chan {cid}"}}
                for cid in channels]})
        if url.endswith("/channels"):
            ids = str(params.get("id") or params.get("forHandle") or "")
            wanted = [c for c in ids.split(",") if c in channels] or list(channels)[:1]
            return FakeResponse(200, {"items": [{
                "id": cid, "snippet": {"title": f"chan {cid}", "customUrl": f"@{cid}"},
                "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}},
                "statistics": {"subscriberCount": "1000", "videoCount": "200"},
            } for cid in wanted]})
        if url.endswith("/playlistItems"):
            channel = str(params.get("playlistId") or "").replace("UU", "")
            latest = params.get("maxResults") == 20
            items = titles_by_channel.get(channel, [])[: (3 if latest else 50)]
            return FakeResponse(200, {"items": [
                {"contentDetails": {"videoId": vid},
                 "snippet": {"title": title, "channelId": channel,
                             "publishedAt": "2026-10-31T07:00:00Z"}}
                for vid, title in items], "nextPageToken": None})
        if url.endswith("/videos"):
            owner = {vid: channel for channel, items in titles_by_channel.items()
                     for vid, _title in items}
            out = []
            for vid in str(params.get("id", "")).split(","):
                item = _video(vid, "2026-10-31T07:00:00Z", 24, "50")
                item["snippet"]["channelId"] = owner.get(vid, "UCaaa")
                item["snippet"]["channelTitle"] = owner.get(vid, "UCaaa")
                out.append(item)
            return FakeResponse(200, {"items": out})
        if "/shorts/" in url:
            return FakeResponse(200, None, text='<link rel="canonical" '
                                                f'href="{url}">')
        raise AssertionError(url)

    return handler


def test_youtube_discovery_persists_provenance_and_balances_caps(tmp_path):
    channels = {"UCaaa": "a", "UCbbb": "b", "UCccc": "c"}
    titles = {
        "UCaaa": [(f"a{i}", "cs2 clutch 4k mirage") for i in range(6)],
        "UCbbb": [(f"b{i}", "counter-strike 2 ace nuke") for i in range(6)],
        "UCccc": [(f"c{i}", "minecraft survival episode") for i in range(6)],
    }
    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["@cs2povarchive"],
        "max_pages": 1, "max_posts": 10, "per_creator_max": 2,
        "discovery": {"enabled": True, "queries": ["cs2 shorts"],
                      "max_creators": 3, "min_relevance": 0.5},
        "own_channel_handle": "@cs2povarchive"}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(
            config, store, "youtube", env=env,
            client=FakeClient(_discovery_handler(channels, titles)),
            clock=lambda: FIXED)
        assert summary["status"] == "ok", summary
        discovered = {row["creator_id"]: dict(row) for row in
                      store.conn.execute("SELECT * FROM discovered_creators")}
        assert set(discovered) == {"UCaaa", "UCbbb", "UCccc"}
        assert discovered["UCaaa"]["accepted"] == 1
        assert discovered["UCbbb"]["accepted"] == 1
        assert discovered["UCccc"]["accepted"] == 0  # irrelevant catalogue
        run = store.conn.execute("SELECT * FROM discovery_runs").fetchone()
        assert run["accepted"] == 2 and run["candidates"] == 3
        notes = json.loads(store.conn.execute(
            "SELECT notes FROM collection_runs").fetchone()[0])
        coverage = {row["creator_id"]: row for row in notes["creator_coverage"]}
        assert coverage["UCaaa"]["collected"] == 2
        assert coverage["UCaaa"]["truncated_by"] == "per_creator_cap"
        assert "UCccc" not in coverage  # rejected creator is never collected
        per_creator = {}
        for row in store.conn.execute("SELECT creator_id, COUNT(*) n FROM posts "
                                      "GROUP BY creator_id"):
            per_creator[row["creator_id"]] = row["n"]
        assert all(count <= 2 for count in per_creator.values())
    finally:
        store.close()


def test_dormant_discovered_channel_is_labelled_not_silently_empty(tmp_path):
    """A relevant channel whose whole catalogue predates the window is reported."""
    channels = {"UCdead": "dead"}
    titles = {"UCdead": [(f"d{i}", "cs2 clutch shorts") for i in range(5)]}

    def handler(url, params, follow):
        if url.endswith("/channels"):
            return FakeResponse(200, {"items": [{
                "id": "UCdead", "snippet": {"title": "dead channel"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UUdead"}},
                "statistics": {"subscriberCount": "10", "videoCount": "5"}}]})
        if url.endswith("/playlistItems"):
            return FakeResponse(200, {"items": [
                {"contentDetails": {"videoId": vid},
                 "snippet": {"title": title, "channelId": "UCdead",
                             "publishedAt": "2024-04-07T09:00:44Z"}}
                for vid, title in titles["UCdead"]], "nextPageToken": None})
        if url.endswith("/videos"):
            out = []
            for vid in str(params.get("id", "")).split(","):
                item = _video(vid, "2024-04-07T09:00:44Z", 24, "50")
                item["snippet"]["channelId"] = "UCdead"
                out.append(item)
            return FakeResponse(200, {"items": out})
        if "/shorts/" in url:
            return FakeResponse(200, None, text='<link rel="canonical" '
                                                f'href="{url}">')
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api", "creators": ["UCdead"], "max_pages": 1,
        "max_posts": 10, "per_creator_max": 5}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "ok"
        assert store.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        notes = json.loads(store.conn.execute(
            "SELECT notes FROM collection_runs").fetchone()[0])
        covered = notes["creator_coverage"][0]
        assert covered["truncated_by"] == "inactive_outside_window"
        assert covered["filtered_outside_window"] == 5
        assert covered["newest_upload_at"].startswith("2024-04-07")
    finally:
        store.close()


def test_discovery_activity_requirement_persists_recency(tmp_path):
    from cs2archive.shorts.social.discovery import discover_youtube
    from cs2archive.shorts.social.context import PlatformContext
    from cs2archive.shorts.social.safety import RateLimiter

    def handler(url, params, follow):
        if url.endswith("/search"):
            return FakeResponse(200, {"items": [
                {"id": {"channelId": "UCfresh"}, "snippet": {"title": "fresh"}},
                {"id": {"channelId": "UCstale"}, "snippet": {"title": "stale"}}]})
        if url.endswith("/channels"):
            ids = str(params.get("id") or "").split(",")
            return FakeResponse(200, {"items": [{
                "id": cid, "snippet": {"title": cid},
                "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}},
                "statistics": {"subscriberCount": "10", "videoCount": "5"},
            } for cid in ids if cid.startswith("UC")]})
        if url.endswith("/playlistItems"):
            cid = str(params.get("playlistId")).replace("UU", "")
            published = ("2026-10-31T00:00:00Z" if cid == "UCfresh"
                         else "2024-01-01T00:00:00Z")
            return FakeResponse(200, {"items": [
                {"snippet": {"title": "cs2 ace", "publishedAt": published}}]})
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api",
        "discovery": {"enabled": True, "queries": ["x"], "max_creators": 5,
                      "min_relevance": 0.5, "require_recent_activity": True}}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        ctx = PlatformContext(store=store, run_id="r1", platform="youtube",
                              config=config.platform("youtube"), env=env,
                              rate=RateLimiter(0, 10), client=FakeClient(handler),
                              window_start="2026-08-01T00:00:00Z")
        info = discover_youtube(ctx, queries=["x"], max_creators=5,
                                min_relevance=0.5,
                                window_start="2026-08-01T00:00:00Z")
        assert [row["creator_id"] for row in info["selected"]] == ["UCfresh"]
        rows = {row["creator_id"]: dict(row) for row in store.conn.execute(
            "SELECT * FROM discovered_creators")}
        assert rows["UCfresh"]["accepted"] == 1
        assert rows["UCstale"]["accepted"] == 0
        fresh_evidence = json.loads(rows["UCfresh"]["evidence"])
        assert fresh_evidence["active_in_window"] is True
        assert fresh_evidence["newest_upload_at"].startswith("2026-10-31")
        stale_evidence = json.loads(rows["UCstale"]["evidence"])
        assert stale_evidence["active_in_window"] is False
        assert stale_evidence["newest_upload_at"].startswith("2024-01-01")
        run = store.conn.execute("SELECT * FROM discovery_runs").fetchone()
        notes = json.loads(run["notes"])
        assert notes["rejected_dormant"] == 1
        assert notes["require_recent_activity"] is True
    finally:
        store.close()


def test_own_channel_paginates_fully_while_competitors_stay_bounded(tmp_path):
    """Own catalogue completeness must not raise the quota cost for rivals."""
    channels = {"UCown": 0, "UCrival": 0}
    pages = {}
    own_dates = ["2026-11-0%dT07:00:00Z" % (d + 1) for d in range(4)] +                 ["2025-01-0%dT07:00:00Z" % (d + 1) for d in range(6)]

    def handler(url, params, follow):
        if url.endswith("/channels"):
            ids = str(params.get("id") or params.get("forHandle") or "")
            wanted = [c for c in ("UCown", "UCrival") if c in ids] or ["UCown"]
            return FakeResponse(200, {"items": [{
                "id": cid, "snippet": {"title": cid},
                "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}},
                "statistics": {"subscriberCount": "10", "videoCount": "999"}}
                for cid in wanted]})
        if url.endswith("/playlistItems"):
            cid = str(params.get("playlistId")).replace("UU", "")
            page = pages.get(cid, 0)
            pages[cid] = page + 1
            published = (own_dates[page] if cid == "UCown"
                         else "2026-10-31T07:00:00Z")
            vid = f"{cid}-v{page}"
            return FakeResponse(200, {
                "items": [{"contentDetails": {"videoId": vid},
                           "snippet": {"title": "cs2 4k mirage",
                                       "channelId": cid,
                                       "publishedAt": published}}],
                "nextPageToken": ("more" if page < 9 else None)})
        if url.endswith("/videos"):
            out = []
            for vid in str(params.get("id", "")).split(","):
                item = _video(vid, "2026-10-31T07:00:00Z", 24, "50")
                item["snippet"]["channelId"] = vid.rsplit("-v", 1)[0]
                out.append(item)
            return FakeResponse(200, {"items": out})
        if "/shorts/" in url:
            return FakeResponse(200, None, text='<link rel="canonical" '
                                                f'href="{url}">')
        raise AssertionError(url)

    config = make_config(tmp_path, {"youtube": {
        "enabled": True, "mode": "api",
        "creators": ["UCown", "UCrival"], "max_pages": 2, "own_max_pages": 10,
        "per_creator_max": 3, "max_posts": 200, "own_channel_id": "UCown"}})
    env = Env(youtube_api_key="k")
    store = SocialStore(config.database, secrets=env.secrets())
    try:
        summary = collect_platform(config, store, "youtube", env=env,
                                   client=FakeClient(handler), clock=lambda: FIXED)
        assert summary["status"] == "ok", summary
        assert pages["UCown"] == 10      # full own history
        assert pages["UCrival"] == 2     # competitor cap unchanged
        notes = json.loads(store.conn.execute(
            "SELECT notes FROM collection_runs").fetchone()[0])
        coverage = {row["creator_id"]: row for row in notes["creator_coverage"]}
        assert coverage["UCown"]["own_channel"] is True
        assert coverage["UCown"]["page_limit"] == 10
        # per_creator_max never truncates the home channel
        assert coverage["UCown"]["collected"] == 10
        assert coverage["UCown"]["truncated_by"] != "per_creator_cap"
        assert coverage["UCrival"]["own_channel"] is False
        assert coverage["UCrival"]["page_limit"] == 2
        assert coverage["UCrival"]["collected"] <= 3  # balanced cap still applies
        own_posts = store.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE creator_id='UCown'").fetchone()[0]
        # 10 pages, including 6 pre-window uploads: the home channel is complete.
        assert own_posts == 10
        rival_posts = store.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE creator_id='UCrival'").fetchone()[0]
        assert rival_posts == 2  # competitor window/cap behaviour unchanged
    finally:
        store.close()
