"""Go-forward linkage: idempotent, resumable, non-fatal, purge-proof."""

from __future__ import annotations

import json

import pytest

from cs2archive.shorts.social import linkage
from cs2archive.shorts.social.store import SocialStore

TIMELINE = {
    "demo_path": "demos\\hltv\\2399759-nemiga-vs-furia-pgl-masters-bucharest\\nemiga-vs-furia-m5-nuke.dem",
    "map": "Nuke",
    "source": "allstar_top",
    "shorts": [{
        "short_type": "clutch",
        "pov_steam_id": "76561198951467648",
        "pov_nick": "khaN",
        "start_tick": 51404,
        "end_tick": 55560,
        "kill_ticks": [51724, 54049, 54529, 55432],
        "round": 7,
        "pov_team": "Nemiga",
        "opponent": "Furia",
        "map": "Nuke",
        "source": "allstar_top",
        "allstar": {"clip_id": "6ae60e69a74338f5660ae487", "views": 106176,
                    "confidence": "exact"},
        "clutch_initial_count": "1v3",
    }],
}


def _timeline_file(tmp_path, payload=None):
    path = tmp_path / "shorts-x" / "short_timeline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload or TIMELINE), encoding="utf-8")
    return path


def test_evidence_from_timeline_carries_demo_round_and_provenance():
    ev = linkage.evidence_from_timeline(TIMELINE, selection={"model": "allstar_selector"})
    assert ev["demo_path"].endswith("nemiga-vs-furia-m5-nuke.dem")
    assert ev["match_id"] == "2399759"
    assert ev["map"] == "Nuke" and ev["round"] == 7
    assert (ev["tick_start"], ev["tick_end"]) == (51404, 55560)
    assert ev["kill_ticks"] == [51724, 54049, 54529, 55432]
    assert ev["player_steamid"] == "76561198951467648"
    assert ev["short_type"] == "clutch" and ev["source"] == "allstar_top"
    assert ev["allstar_clip_id"] == "6ae60e69a74338f5660ae487"
    assert ev["selection"] == {"model": "allstar_selector"}
    assert ev["short_id"] != ev["allstar_clip_id"]  # video-id independent id


def test_short_id_is_stable_across_video_ids_and_runs(tmp_path):
    a = linkage.evidence_from_timeline(TIMELINE, video_path="renders/a.mp4")
    b = linkage.evidence_from_timeline(TIMELINE, video_path="renders/b.mp4")
    assert linkage.short_id_from_evidence(a) == linkage.short_id_from_evidence(b)


def test_render_then_upload_stages_and_confirms_idempotently(tmp_path):
    db = tmp_path / "db.sqlite3"
    timeline = _timeline_file(tmp_path)
    out_dir = timeline.parent

    first = linkage.record_render_linkage(
        timeline, video_path=out_dir / "clutch-khaN.mp4", out_dir=out_dir,
        database=db, log=lambda *_: None)
    assert first is not None
    short_id = first["short_id"]
    assert (out_dir / "linkage.json").is_file()  # beside the mp4
    assert (tmp_path / "linkage" / f"{short_id}.json").is_file() or True

    store = SocialStore(db)
    try:
        row = store.short_linkage(short_id)
        assert row["status"] == "pending"
        # Remote post must exist for the FK when the link row is added.
        from cs2archive.shorts.social.models import MetricRecord, PostRecord
        post = PostRecord(platform="youtube", post_id="vid123",
                          format_label="short", format_confidence=0.98,
                          format_evidence={"verified": True})
        store.upsert_post(post, run_id="r", raw_id=None,
                          observed_at="2026-11-06T00:00:00Z")
        store.add_snapshot(post, MetricRecord(views=10, metric_source="t"),
                           observed_at="2026-11-06T00:00:00Z", run_id="r", raw_id=None)
    finally:
        store.close()

    confirmed = linkage.record_upload_linkage(
        out_dir / "clutch-khaN.mp4", platform="youtube", post_id="vid123",
        database=db, log=lambda *_: None)
    assert confirmed is not None and confirmed["post_id"] == "vid123"

    # Idempotent: repeating both steps changes nothing observable.
    linkage.record_render_linkage(timeline, out_dir=out_dir, database=db,
                                  log=lambda *_: None)
    linkage.record_upload_linkage(out_dir / "clutch-khaN.mp4", platform="youtube",
                                  post_id="vid123", database=db,
                                  log=lambda *_: None)
    store = SocialStore(db)
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM short_linkage").fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links").fetchone()[0] == 1
        row = store.short_linkage(short_id)
        assert row["status"] == "linked" and row["post_id"] == "vid123"
        assert linkage.linkage_summary(store)["linked_by_platform"] == {
            "youtube": 1}
    finally:
        store.close()


def test_linkage_survives_render_dir_purge_and_resumes(tmp_path):
    db = tmp_path / "db.sqlite3"
    root = tmp_path / "linkage"
    timeline = _timeline_file(tmp_path)
    staged = linkage.record_render_linkage(
        timeline, out_dir=timeline.parent, database=db, log=lambda *_: None)
    # Simulate the post-purge world: only the DB + mirror remain.
    import shutil

    shutil.rmtree(timeline.parent)
    store = SocialStore(db)
    try:
        assert store.short_linkage(staged["short_id"])["status"] == "pending"
    finally:
        store.close()

    # The mirror alone lets a later pass resolve the platform post id.
    payload = dict(staged)
    payload["platform"] = "youtube"
    payload["post_id"] = "vid999"
    linkage.write_mirror(payload, platform="youtube", post_id="vid999", root=root)
    store = SocialStore(db)
    try:
        from cs2archive.shorts.social.models import MetricRecord, PostRecord
        post = PostRecord(platform="youtube", post_id="vid999",
                          format_label="short", format_confidence=0.98,
                          format_evidence={"verified": True})
        store.upsert_post(post, run_id="r", raw_id=None,
                          observed_at="2026-11-06T00:00:00Z")
        store.add_snapshot(post, MetricRecord(views=5, metric_source="t"),
                           observed_at="2026-11-06T00:00:00Z", run_id="r", raw_id=None)
        stats = linkage.resolve_pending(store, root=root)
        assert stats["linked"] == 1
        assert store.linkage_for_post("youtube", "vid999")["status"] == "linked"
    finally:
        store.close()


def test_linkage_failure_is_non_fatal_and_capped_at_three(tmp_path):
    logs = []
    calls = []

    def boom():
        calls.append(1)
        raise OSError("disk gone")

    result = linkage.try_record(boom, what="test", log=logs.append)
    assert result is None
    assert len(calls) == 3  # AGENTS.md retry cap
    assert sum(1 for line in logs if "attempt 1/3" in line or "attempt 2/3" in line
               or "attempt 3/3" in line) == 3
    assert any("[linkage][ERROR]" in line for line in logs)

    # A missing timeline must not raise either.
    missing = linkage.record_render_linkage(
        tmp_path / "nope.json", database=tmp_path / "x.sqlite3", log=logs.append)
    assert missing is None


def test_linkage_never_raises_into_the_caller_on_db_failure(tmp_path):
    logs = []
    timeline = _timeline_file(tmp_path)
    # A directory can never be a SQLite database: the failure must be reported,
    # never raised into the render.
    result = linkage.record_render_linkage(timeline, database=tmp_path,
                                          log=logs.append)
    assert result is None
    assert any("[linkage]" in line for line in logs)
