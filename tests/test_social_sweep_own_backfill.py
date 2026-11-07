"""Own-channel backfill driver: registry resolution, verification, persistence."""

from __future__ import annotations

import json

from cs2archive.shorts.social.models import MetricRecord, PostRecord
from cs2archive.shorts.social.own_backfill import (
    backfill_own_channel, load_player_registry, resolve_player,
)
from cs2archive.shorts.social.store import SocialStore

OWN = "UCh8PPTy8KFPhxvVZOCE519Q"
STEAM = "76561199032006224"


def _registry(tmp_path):
    path = tmp_path / "player_accounts.json"
    path.write_text(json.dumps([
        {"nickname": "donk", "steam_id": STEAM,
         "steam_url": "https://steamcommunity.com/id/donkk"},
        {"nickname": "s1mple", "steam_id": "76561198034202275"},
    ]), encoding="utf-8")
    return path


def test_resolve_player_prefers_longest_match_and_ignores_substrings(tmp_path):
    registry = load_player_registry(_registry(tmp_path))
    assert resolve_player("donk fast 4k", registry) == (STEAM, "donk")
    assert resolve_player("nobody here 4k", registry) == (None, None)
    # "donkk" must not match the "donk" alias as a substring.
    assert resolve_player("donkk 4k", registry)[1] is None or \
        resolve_player("donkk 4k", registry)[1] == "donkk"


def test_load_player_registry_indexes_vanity_and_faceit_names(tmp_path):
    registry = load_player_registry(_registry(tmp_path))
    assert registry["donk"] == STEAM
    assert registry["donkk"] == STEAM
    assert registry["s1mple"] == "76561198034202275"


def _timeline(tmp_path, tmp_timelines, folder, stem, *, map_name, kills,
              mtime="2026-08-19T00:00:00Z"):
    import os
    from datetime import datetime

    demo = tmp_path / "demos" / folder / f"{stem}.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    demo.write_bytes(b"fake")
    # The published date must be plausible for the demo, so pin the mtime inside
    # the window rather than leaving it at "now".
    stamp = datetime.fromisoformat(mtime.replace("Z", "+00:00")).timestamp()
    os.utime(demo, (stamp, stamp))
    timeline = {
        "timeline_version": 3, "map": map_name,
        "pros": [{"steam_id": STEAM, "name": "donk"}],
        "kills_all": [{"tick": 1000 + i, "round": 5, "attacker_steam_id": STEAM,
                       "attacker_team_num": 3, "victim_steam_id": f"9{i}"}
                      for i in range(kills)],
        "winner_by_round": {5: 3},
        "rounds": [{"round": 5, "start_tick": 900, "end_tick": 2000}],
    }
    path = tmp_path / "timelines" / f"{folder}__{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(timeline), encoding="utf-8")
    return demo


def _config(tmp_path):
    from cs2archive.shorts.social.config import load_config

    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "database": str(tmp_path / "db.sqlite3"),
        "raw_root": str(tmp_path / "raw"),
        "platforms": {"youtube": {"enabled": True, "mode": "api",
                                  "own_channel_id": OWN}},
    }), encoding="utf-8")
    return load_config(path)


def _own_post(store, post_id, title, published="2026-08-20T00:00:00Z"):
    post = PostRecord(platform="youtube", post_id=post_id, creator_id=OWN,
                      creator_name="CS2 Archive", caption=title,
                      published_at=published, format_label="short",
                      format_confidence=0.98, format_evidence={"verified": True})
    store.upsert_post(post, run_id="r", raw_id=None, observed_at=published)
    store.add_snapshot(post, MetricRecord(views=100, metric_source="t"),
                       observed_at=published, run_id="r", raw_id=None)


def test_backfill_links_only_a_verified_single_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr("cs2archive.shorts.social.own_backfill.PLAYER_ACCOUNTS",
                        _registry(tmp_path))
    _timeline(tmp_path, None, "2399001-donk-vs-spirit-m1-inferno", "m1-inferno",
              map_name="Inferno", kills=4)
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        _own_post(store, "v_linked", "donk's 4K vs Spirit on Inferno #cs2")
        _own_post(store, "v_nomap", "donk fast 4k")
        _own_post(store, "v_nosig", "insane play on Inferno by donk")
        stats = backfill_own_channel(
            store, config=config, demos_root=tmp_path / "demos",
            timeline_dir=tmp_path / "timelines", prefer_ledger=False,
            log=lambda *_: None)
        assert stats["by_status"].get("linked") == 1, stats["by_status"]
        assert stats["by_status"].get("no_map") == 1
        assert stats["by_status"].get("no_candidate") == 1
        row = store.linkage_for_post("youtube", "v_linked")
        assert row["status"] == "linked"
        assert row["round"] == 5 and row["kill_count"] == 4
        assert row["match_id"] == "2399001"
        assert row["demo_path"].endswith("m1-inferno.dem")
        # A verified link enters the trainer's exact-link path.
        links = [dict(r) for r in store.conn.execute(
            "SELECT * FROM clip_identity_links WHERE post_id='v_linked'")]
        assert links and links[0]["method"] == "own_channel_demo_backfill"
        assert links[0]["ambiguous"] == 0 and links[0]["confidence"] == 1.0
        # Non-linked outcomes never create identity links.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links").fetchone()[0] == 1
    finally:
        store.close()


def test_backfill_marks_ambiguity_instead_of_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr("cs2archive.shorts.social.own_backfill.PLAYER_ACCOUNTS",
                        _registry(tmp_path))
    _timeline(tmp_path, None, "2399002-donk-vs-a-m1-inferno", "m1-inferno",
              map_name="Inferno", kills=4)
    _timeline(tmp_path, None, "2399003-donk-vs-b-m1-inferno", "m1-inferno",
              map_name="Inferno", kills=4)
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        _own_post(store, "v_amb", "donk casually drops a 4K on Inferno #cs2")
        stats = backfill_own_channel(
            store, config=config, demos_root=tmp_path / "demos",
            timeline_dir=tmp_path / "timelines", prefer_ledger=False,
            log=lambda *_: None)
        assert stats["by_status"].get("ambiguous") == 1, stats["by_status"]
        row = store.linkage_for_post("youtube", "v_amb")
        assert row["status"] == "ambiguous"
        assert "exactly one" in row["reason"]
        evidence = json.loads(row["evidence"])
        assert evidence["candidate_count"] == 2
        assert evidence["status"] == "ambiguous"
        assert len(evidence["candidates"]) == 2
        assert store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links").fetchone()[0] == 0
    finally:
        store.close()


def test_backfill_reports_no_demo_corpus_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr("cs2archive.shorts.social.own_backfill.PLAYER_ACCOUNTS",
                        _registry(tmp_path))
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        _own_post(store, "v_nodemo", "donk's 4K on Overpass #cs2")
        stats = backfill_own_channel(
            store, config=config, demos_root=tmp_path / "empty",
            timeline_dir=tmp_path / "timelines", prefer_ledger=False,
            log=lambda *_: None)
        assert stats["by_status"].get("no_candidate") == 1
        assert stats["demos"] == 0
    finally:
        store.close()


def test_backfill_is_repeatable_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr("cs2archive.shorts.social.own_backfill.PLAYER_ACCOUNTS",
                        _registry(tmp_path))
    _timeline(tmp_path, None, "2399004-donk-vs-spirit-m1-anubis", "m1-anubis",
              map_name="Anubis", kills=5)
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        _own_post(store, "v_ace", "donk ACE vs Spirit on Anubis #cs2")
        for _ in range(2):
            backfill_own_channel(store, config=config,
                                 demos_root=tmp_path / "demos",
                                 timeline_dir=tmp_path / "timelines",
                                 prefer_ledger=False, log=lambda *_: None)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM short_linkage").fetchone()[0] == 1
        assert store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links").fetchone()[0] == 1
        row = store.linkage_for_post("youtube", "v_ace")
        assert row["status"] == "linked" and row["kill_count"] == 5
    finally:
        store.close()


def _ledger(tmp_path, entries):
    path = tmp_path / "shorts_render_selection.json"
    path.write_text(json.dumps({"version": 2, "entries": entries}), encoding="utf-8")
    return path


def test_ledger_entries_parse_demo_stem_player_and_signature(tmp_path):
    from cs2archive.shorts.social.own_backfill import ledger_entries

    path = _ledger(tmp_path, {
        "k1": {"timeline": r"D:\p\renders\pov-legacy-vs-1win-m1-dust2_Ax1Le\shorts"
                            r"\shorts-4k_multikill-Ax1Le-t54679\short_timeline.json",
               "features": {"steamid": STEAM, "opponent": "Legacy", "kinds": ["4k"]},
               "reserved_day": "2026-10-06", "predicted_views": 22715.0},
    })
    entries = ledger_entries(path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["demo_stem"] == "legacy-vs-1win-m1-dust2"
    assert entry["player_nick"] == "Ax1Le"
    assert entry["player_steamid"] == STEAM
    assert entry["map"] == "dust2"
    assert entry["kills"] == 4 and entry["kill_kind"] == "4k"
    assert entry["tick"] == 54679  # real ledger value, parsed from the token
    assert entry["reserved_day"] == "2026-10-06"


def test_ledger_entry_is_verified_against_the_local_demo_and_stays_pending(
        tmp_path, monkeypatch):
    from cs2archive.shorts.social.own_backfill import link_ledger

    _timeline(tmp_path, None, "2399005-legacy-vs-1win-m1-dust2",
              "legacy-vs-1win-m1-dust2", map_name="Dust2", kills=4)
    path = _ledger(tmp_path, {
        "k1": {"timeline": r"D:\p\renders\pov-legacy-vs-1win-m1-dust2_Ax1Le\shorts"
                            r"\shorts-4k_multikill-Ax1Le-t1003\short_timeline.json",
               "features": {"steamid": STEAM, "opponent": "Legacy", "kinds": ["4k"]},
               "reserved_day": "2026-10-06", "predicted_views": 22715.0},
    })
    from cs2archive.shorts.social.own_backfill import ledger_entries as le

    entries = le(path)
    assert len(entries) == 1
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        stats = link_ledger(store, entries=entries, demos_root=tmp_path / "demos",
                            timeline_dir=tmp_path / "timelines", run_id="r",
                            build_budget=0, log=lambda *_: None)
        assert stats["verified"] == 1, stats
        assert stats["pending_post_id"] == 1
        row = store.conn.execute(
            "SELECT * FROM short_linkage WHERE source='render_selection_ledger'"
        ).fetchone()
        assert row["status"] == "pending"
        assert row["post_id"] is None            # the ledger has no platform id
        assert row["round"] == 5 and row["kill_count"] == 4
        assert row["demo_path"].endswith("legacy-vs-1win-m1-dust2.dem")
        assert "platform post id" in row["reason"]
    finally:
        store.close()


def test_ledger_entry_without_a_local_demo_is_reported_not_linked(tmp_path):
    from cs2archive.shorts.social.own_backfill import link_ledger, ledger_entries

    path = _ledger(tmp_path, {
        "k2": {"timeline": r"D:\p\renders\pov-gone-vs-x-m1-nuke_Bob\shorts"
                            r"\shorts-ace-Bob-t100\short_timeline.json",
               "features": {"steamid": STEAM, "kinds": ["ace"]}},
    })
    config = _config(tmp_path)
    store = SocialStore(config.database)
    try:
        stats = link_ledger(store, entries=ledger_entries(path),
                            demos_root=tmp_path / "empty",
                            timeline_dir=tmp_path / "timelines", run_id="r",
                            build_budget=0, log=lambda *_: None)
        assert stats["no_demo"] == 1
        row = store.conn.execute("SELECT * FROM short_linkage").fetchone()
        assert row["status"] == "no_demo"
        assert "not in the corpus" in row["reason"]
    finally:
        store.close()
