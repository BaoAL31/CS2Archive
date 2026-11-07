"""CLI end-to-end: bounded run, repeat-safe ingestion, report, honest refusal."""

from __future__ import annotations

import json

from cs2archive.shorts import social_sweep
from cs2archive.shorts.social import collect as collect_module
from cs2archive.shorts.social.context import Env


def _allstar_store(tmp_path):
    path = tmp_path / "allstar.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in [
        {"match_id": "1", "scraped_at": "2026-10-02T02:00:00+00:00",
         "clips": [
             {"clip_id": "c1", "player": "p", "steamid": "1", "views": 100,
              "title": "ACE", "published_at": "2026-10-02T02:00:00+00:00",
              "observed_at": "2026-10-02T02:00:00+00:00", "publication_time_known": True},
             {"clip_id": "c2", "player": "q", "steamid": "2", "views": 2,
              "title": "poor clip", "published_at": "2026-10-02T02:00:00+00:00",
              "observed_at": "2026-10-02T02:00:00+00:00"}]},
    ]) + "\n", encoding="utf-8")
    return path


def _youtube_config(tmp_path) -> object:
    path = tmp_path / "sources_yt.json"
    path.write_text(json.dumps({
        "database": str(tmp_path / "yt.sqlite3"),
        "raw_root": str(tmp_path / "ytraw"),
        "window": {"start": "2026-08-01T00:00:00Z"},
        "rate_limit": {"min_interval_seconds": 0.0, "max_requests_per_run": 10},
        "platforms": {"youtube": {"enabled": True, "mode": "api",
                                  "creators": ["@chan"], "max_posts": 5}},
        "training": {"output": str(tmp_path / "yt_train.json")},
    }), encoding="utf-8")
    return path


class _Always500:
    def get(self, url, params=None, headers=None, timeout=None, follow_redirects=True):
        from test_social_sweep_platforms import FakeResponse
        return FakeResponse(500, None, "boom")


def _config(tmp_path) -> object:
    store_file = _allstar_store(tmp_path)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "database": str(tmp_path / "db.sqlite3"),
        "raw_root": str(tmp_path / "raw"),
        "window": {"start": "2026-08-01T00:00:00Z"},
        "rate_limit": {"min_interval_seconds": 0.0, "max_requests_per_run": 10},
        "platforms": {
            "allstar": {"enabled": True, "mode": "local_store_import",
                        "store": str(store_file), "max_posts": 10},
            "tiktok": {"enabled": True, "mode": "oembed", "video_urls": []},
            "reddit": {"enabled": False, "communities": ["GlobalOffensive"]},
        },
        "foundation": {"dir": str(tmp_path / "foundation"),
                       "allstar_store": str(store_file)},
        "training": {"min_rows_per_platform": 25,
                     "output": str(tmp_path / "train.json")},
    }), encoding="utf-8")
    return path


def test_collect_is_repeat_safe_and_reports_blocked_platforms(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)

    assert social_sweep.main(["collect", "--config", str(config), "--no-enrich"]) == 0
    first = capsys.readouterr().out
    assert "[allstar] ok" in first
    assert "[tiktok] blocked" in first
    assert "[reddit] unavailable" in first

    assert social_sweep.main(["collect", "--config", str(config), "--no-enrich"]) == 0
    capsys.readouterr()

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    try:
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        snapshots = conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
        statuses = [row[0] for row in conn.execute(
            "SELECT status FROM collection_runs ORDER BY run_id")]
    finally:
        conn.close()
    assert posts == 2
    assert snapshots == 2  # re-ingest of the preserved payload adds no duplicate
    assert runs == 6  # three platforms per sweep, both sweeps recorded
    assert statuses.count("ok") == 2
    assert statuses.count("blocked") == 2
    assert statuses.count("unavailable") == 2


def test_report_prints_coverage_and_metrics(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    assert social_sweep.main(["report", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    allstar = report["coverage"]["allstar"]
    assert allstar["posts"] == 2
    assert allstar["metric_non_null"]["views"] == 2
    assert allstar["metric_non_null"]["likes"] == 0
    assert allstar["declared"]["status"] == "partial"
    assert report["coverage"]["tiktok"]["declared"]["status"] == "blocked"
    assert any("Allstar" in item for item
               in report["coverage"]["allstar"]["declared"]["requirements"])


def test_train_refuses_without_flag_or_enough_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    assert social_sweep.main(["train", "--config", str(config)]) == 1
    assert "experimental" in capsys.readouterr().out
    assert social_sweep.main(
        ["train", "--config", str(config), "--experimental", "--min-rows", "100"]) == 1
    out = capsys.readouterr().out
    assert "refused" in out
    assert "no platform had enough verified rows" in out


def test_collect_failure_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_module, "env_from_settings",
                        lambda: Env(youtube_api_key="k"))
    monkeypatch.setattr(collect_module, "_load_client", lambda: _Always500())
    config = _youtube_config(tmp_path)
    assert social_sweep.main(["collect", "--config", str(config)]) == 1
    out = capsys.readouterr().out
    assert "[youtube] failed" in out


def test_dry_run_makes_no_network_calls_and_records_statuses(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    monkeypatch.setattr(collect_module, "_load_client",
                        lambda: (_ for _ in ()).throw(AssertionError("network used")))
    assert social_sweep.main(["collect", "--config", str(config), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry_run" in out


def test_report_reports_snapshot_intervals_and_hypotheses(tmp_path, monkeypatch, capsys):
    from cs2archive.shorts.social import collect as collect_module
    from cs2archive.shorts.social.context import Env

    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    assert social_sweep.main(["report", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    snapshot_stats = report["snapshots"]["allstar"]
    assert snapshot_stats["snapshots"] == snapshot_stats["posts"]
    assert snapshot_stats["posts_with_multiple_snapshots"] == 0
    hypotheses = report["hypotheses"]
    # No foundation clips are staged in this fixture, so no exact links exist.
    assert hypotheses["exact_links"]["links"] == 0
    assert hypotheses["unlinked_posts"]["allstar"] == 2
    assert "grouping_rule" in hypotheses
    assert "why_unmatched" in hypotheses
    assert report["quota"]["units_by_platform"]
    assert report["interpretation"]["can_explain_cross_platform_gameplay"] is False


def test_report_counts_repeated_snapshots_with_real_interval(tmp_path, monkeypatch, capsys):
    from cs2archive.shorts.social import collect as collect_module
    from cs2archive.shorts.social.context import Env
    from cs2archive.shorts.social.models import MetricRecord, PostRecord
    from cs2archive.shorts.social.store import SocialStore

    config = _config(tmp_path)
    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        post = PostRecord(platform="allstar", post_id="c1", caption="x",
                          published_at=None)
        store.add_snapshot(post, MetricRecord(views=9, metric_source="allstar_store"),
                           observed_at="2026-11-06T09:00:00Z", run_id="r2", raw_id=None)
    finally:
        store.close()
    social_sweep.main(["report", "--config", str(config), "--json"])
    report = json.loads(capsys.readouterr().out)
    entry = report["snapshots"]["allstar"]
    assert entry["posts_with_multiple_snapshots"] == 1
    assert entry["median_interval_hours_first_to_last"] is not None
    assert entry["median_interval_hours_first_to_last"] > 0


def test_report_includes_linkage_analytics_and_ceiling(tmp_path, monkeypatch, capsys):
    from cs2archive.shorts.social import collect as collect_module
    from cs2archive.shorts.social.context import Env
    from cs2archive.shorts.social.store import SocialStore

    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_short_linkage(
            {"short_id": "s1", "platform": "youtube", "post_id": "vid1",
             "demo_path": "demos/x.dem", "map": "Inferno", "round": 3,
             "kill_ticks": [1, 2, 3, 4], "kill_count": 4, "short_type": "4k"},
            status="linked", run_id="r")
        store.upsert_short_linkage(
            {"short_id": "s2", "demo_path": "", "map": "Mirage",
             "reason": "2 qualifying rounds matched"},
            status="ambiguous", run_id="r")
    finally:
        store.close()
    assert social_sweep.main(["report", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["linkage"]["by_status"]["linked"]["with_post_id"] == 1
    assert report["linkage"]["by_status"]["ambiguous"]["rows"] == 1
    assert "qualifying rounds" in " ".join(report["linkage"]["reasons"])
    assert "analytics" in report and "token_path" in report["analytics"]
    assert "competitor_summary" in report["ceiling"]
    # Text form carries the same sections.
    assert social_sweep.main(["report", "--config", str(config)]) == 0
    text = capsys.readouterr().out
    assert "short → demo linkage" in text
    assert "demo-linkage ceiling" in text
    assert "own-channel analytics" in text


def test_report_surfaces_moment_dossiers_when_present(tmp_path, monkeypatch, capsys):
    from cs2archive.shorts.social import collect as collect_module
    from cs2archive.shorts.social.context import Env

    monkeypatch.setattr(collect_module, "env_from_settings", lambda: Env())
    config = _config(tmp_path)
    social_sweep.main(["collect", "--config", str(config), "--no-enrich"])
    capsys.readouterr()
    # Absent: the report points at the command instead of pretending coverage.
    assert social_sweep.main(["report", "--config", str(config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dossiers"]["present"] is False

    foundation = tmp_path / "foundation"          # configured foundation dir
    dossiers = foundation / "dossiers"
    dossiers.mkdir(parents=True)
    (dossiers / "INDEX.json").write_text(json.dumps({
        "dossiers_version": 1, "built": 3, "skipped_current": 1, "failed": 0,
        "by_source": {"allstar_alignment": 3},
        "availability": {"dossiers": 4, "present_share": {"chronology": 1.0}},
    }), encoding="utf-8")
    (dossiers / "abc.txt").write_text("# Moment abc", encoding="utf-8")
    assert social_sweep.main(["report", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dossiers"]["present"] is True
    assert report["dossiers"]["availability"]["dossiers"] == 4
    assert social_sweep.main(["report", "--config", str(config)]) == 0
    text = capsys.readouterr().out
    assert "moment dossiers:" in text and "feature presence" in text
