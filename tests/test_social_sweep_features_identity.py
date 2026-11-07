"""Feature extraction and identity linking: measured vs inferred vs missing."""

from __future__ import annotations

import json

import pandas as pd

from cs2archive.shorts.social.features import attach_features, summarize_features
from cs2archive.shorts.social.identity import build_foundation_index, group_key, link_posts
from cs2archive.shorts.social.models import MetricRecord, PostRecord
from cs2archive.shorts.social.store import SocialStore

STEAM = "76561199032006224"


def _write_clip(root, clip_id: str, *, match_id="2389644", map_name="Inferno",
                player="kyousuke", round_no=12, killed=2, quarantined=False,
                kinds=None):
    clip = root / "clips" / clip_id
    clip.mkdir(parents=True)
    tick_start, first, second, tick_end = 86239, 86559, 86641, 86862
    (clip / "alignment.json").write_text(json.dumps({
        "clip_id": clip_id, "demo": "demos/x.dem", "match_id": match_id,
        "steamid": STEAM, "player": player, "round": round_no,
        "tick_start": tick_start, "tick_end": tick_end,
        "round_tick_start": 80267, "round_tick_end": 87598,
        "kill_ticks": [first, second][:killed], "explicit_kills": killed,
        "demo_kills": killed, "confidence": "exact", "method": "steamid+map+round+kills",
        "quarantined": quarantined, "kinds": kinds or ["3k"],
    }), encoding="utf-8")
    (clip / "match.json").write_text(json.dumps({
        "match_id": match_id, "map": map_name, "stage": "group"}), encoding="utf-8")
    (clip / "raw_allstar.json").write_text(json.dumps({
        "clip_id": clip_id, "player": player, "kinds": kinds or ["3k"],
        "match_id": match_id}), encoding="utf-8")
    kills = [{"tick": t, "round": round_no, "attacker": player,
              "attacker_steam_id": STEAM, "victim": f"v{i}",
              "victim_steam_id": f"76{i}", "weapon": ["ak47", "m4a1"][i % 2],
              "headshot": i % 2 == 0, "is_bomb": False,
              "kill_event": {"headshot": i % 2 == 0}}
             for i, t in enumerate([first, second][:killed])]
    events = {
        "kills": kills,
        "player_death": kills,
        "player_hurt": [
            {"tick": first - 10, "attacker_steam_id": STEAM, "user_steamid": "761",
             "dmg_health": 40, "weapon": "ak47"},
            {"tick": second - 8, "attacker_steam_id": STEAM, "user_steamid": "762",
             "dmg_health": 60, "weapon": "m4a1"},
        ],
        "weapon_fire": [
            {"tick": first - 4, "user_steamid": STEAM, "weapon": "weapon_ak47"},
            {"tick": first - 2, "user_steamid": STEAM, "weapon": "weapon_ak47"},
            {"tick": second - 3, "user_steamid": STEAM, "weapon": "weapon_m4a1"},
            {"tick": first - 30, "user_steamid": STEAM, "weapon": "weapon_flashbang"},
            {"tick": first - 20, "user_steamid": "999", "weapon": "weapon_ak47"},
        ],
    }
    pd.DataFrame([{"key": key, "payload": json.dumps(rows)}
                  for key, rows in events.items()]).to_parquet(
        clip / "events.parquet", index=False)
    return clip


def _post(platform: str, post_id: str, **kwargs) -> PostRecord:
    defaults = dict(platform=platform, post_id=post_id,
                    published_at="2026-10-01T00:00:00Z", caption="clip",
                    creator_name="kyousuke", url=f"https://x/{post_id}")
    defaults.update(kwargs)
    return PostRecord(**defaults)


def _add(store: SocialStore, post: PostRecord) -> None:
    store.upsert_post(post, run_id="run-1", raw_id=None,
                      observed_at=post.published_at or "2026-11-01T00:00:00Z")
    store.add_snapshot(post, MetricRecord(views=10, metric_source="test"),
                       observed_at=post.published_at or "2026-11-01T00:00:00Z",
                       run_id="run-1", raw_id=None)


def test_identity_links_exact_caption_and_ambiguous(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "a" * 24, match_id="m1")
    _write_clip(foundation, "b" * 24, match_id="m2", map_name="Mirage")
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("allstar", "a" * 24, caption="ACE"))
        _add(store, _post("youtube", "vid1", caption=f"great clip {'a' * 24}"))
        _add(store, _post("youtube", "vid2", caption=f"two {'a' * 24} {'b' * 24}"))
        _add(store, _post("reddit", "r1", caption="no identity here"))
        stats = link_posts(store, foundation_dir=foundation, run_id="run-1")
        assert stats["exact"] == 1
        assert stats["caption_id"] == 1
        assert stats["ambiguous"] == 1
        links = {(r["post_id"], r["method"]): dict(r) for r in store.conn.execute(
            "SELECT * FROM clip_identity_links")}
        assert links[("a" * 24, "allstar_clip_id")]["confidence"] == 1.0
        assert links[("a" * 24, "allstar_clip_id")]["map_name"] == "Inferno"
        assert links[("a" * 24, "allstar_clip_id")]["player"] == "kyousuke"
        assert links[("a" * 24, "allstar_clip_id")]["round_no"] == 12
        assert links[("vid1", "clip_id_in_caption")]["match_id"] == "m1"
        assert links[("vid2", "clip_id_in_caption")]["ambiguous"] == 1

        def links_for(post_id):
            return [dict(r) for r in store.conn.execute(
                "SELECT * FROM clip_identity_links WHERE post_id=?", (post_id,))]
        gid, basis = group_key("youtube", "vid1", links_for("vid1"))
        assert (gid, basis) == (f"clip:{'a' * 24}", "foundation_clip")
        # Cross-platform: the same verified clip on two platforms shares a group.
        assert group_key("youtube", "vid1", links_for("vid1"))[0] == \
            group_key("allstar", "a" * 24, links_for("a" * 24))[0]
        ambiguous_gid, ambiguous_basis = group_key("youtube", "vid2", links_for("vid2"))
        assert ambiguous_basis == "post"
        assert ambiguous_gid == "post:youtube:vid2"
        unlinked_gid, _ = group_key("reddit", "r1", links_for("r1"))
        assert unlinked_gid == "post:reddit:r1"
    finally:
        store.close()


def test_foundation_features_are_measured_not_invented(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "a" * 24, killed=2)
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("allstar", "a" * 24))
        _add(store, _post("youtube", "vid9", caption="unlinked"))
        link_posts(store, foundation_dir=foundation, run_id="run-1")
        stats = attach_features(store, run_id="run-1", foundation_dir=foundation)
        assert stats["linked"] == 1
        features = store.features_for("allstar", "a" * 24)
        assert features["kills"]["value_num"] == 2.0
        assert features["kills"]["provenance"] == "measured"
        assert features["headshots"]["value_num"] == 1.0
        assert features["distinct_weapons"]["value_num"] == 2.0
        assert features["damage_dealt"]["value_num"] == 100.0
        assert features["fire_events"]["value_num"] == 3.0   # flashbang excluded
        assert features["hit_events"]["value_num"] == 2.0
        assert features["hit_event_ratio"]["value_num"] == round(2 / 3, 4)
        assert features["round_number"]["value_num"] == 12.0
        assert features["clip_span_seconds"]["value_num"] > 0
        # Not measurable => explicit unavailable row, never a fabricated value.
        assert features["first_action_seconds"]["value_num"] is None
        assert features["first_action_seconds"]["provenance"] == "unavailable"
        assert features["dead_time_seconds"]["provenance"] == "unavailable"
        # Unlinked post keeps null gameplay rows with a reason.
        unlinked = store.features_for("youtube", "vid9")
        assert unlinked["kills"]["value_num"] is None
        assert unlinked["kills"]["provenance"] == "unavailable"
        assert "no verified identity link" in unlinked["kills"]["evidence"]
        summary = summarize_features(store)
        assert summary["kills"]["present"] == 1
        assert summary["kills"]["missing"] == 1
    finally:
        store.close()


def test_ambiguous_or_tied_links_never_attach_gameplay(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "a" * 24, match_id="m1")
    _write_clip(foundation, "b" * 24, match_id="m2")
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("youtube", "amb", caption="ambiguous"))
        _add(store, _post("youtube", "tied", caption="tied"))
        from cs2archive.shorts.social.models import LinkRecord
        store.upsert_link(LinkRecord(
            platform="youtube", post_id="amb", foundation_clip_id="a" * 24,
            match_id="m1", method="clip_id_in_caption", confidence=0.95,
            ambiguous=True), run_id="run-1")
        # Two equally-verified clips for one post: identity is not unambiguous.
        for clip in ("a" * 24, "b" * 24):
            store.upsert_link(LinkRecord(
                platform="youtube", post_id="tied", foundation_clip_id=clip,
                match_id="m1" if clip[0] == "a" else "m2",
                method="allstar_clip_id", confidence=1.0, ambiguous=False),
                run_id="run-1")
        stats = attach_features(store, run_id="run-1", foundation_dir=foundation)
        assert stats["rejected_multiple_clips"] == 1
        for post_id in ("amb", "tied"):
            features = store.features_for("youtube", post_id)
            assert features["kills"]["provenance"] == "unavailable"
            assert features["kills"]["value_num"] is None
        assert "multiple verified clips" in store.features_for(
            "youtube", "tied")["kills"]["evidence"]
    finally:
        store.close()


def test_partial_alignment_is_rejected_as_unverified(tmp_path):
    foundation = tmp_path / "foundation"
    clip = _write_clip(foundation, "e" * 24)
    align = json.loads((clip / "alignment.json").read_text(encoding="utf-8"))
    align["confidence"] = "partial"
    (clip / "alignment.json").write_text(json.dumps(align), encoding="utf-8")
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("allstar", "e" * 24))
        link_posts(store, foundation_dir=foundation, run_id="run-1")
        stats = attach_features(store, run_id="run-1", foundation_dir=foundation)
        assert stats["rejected_not_exact"] == 1
        features = store.features_for("allstar", "e" * 24)
        assert features["kills"]["value_num"] is None
        assert "alignment confidence is partial" in features["kills"]["evidence"]
    finally:
        store.close()


def test_missing_headshot_flag_is_unknown_not_false(tmp_path):
    foundation = tmp_path / "foundation"
    clip = _write_clip(foundation, "f" * 24, killed=2)
    import pandas as pd
    frame = pd.read_parquet(clip / "events.parquet")
    kills = json.loads(frame[frame["key"] == "kills"].iloc[0]["payload"])
    kills[0].pop("headshot")  # flag missing on one kill
    frame.loc[frame["key"] == "kills", "payload"] = json.dumps(kills)
    frame.to_parquet(clip / "events.parquet", index=False)
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("allstar", "f" * 24))
        link_posts(store, foundation_dir=foundation, run_id="run-1")
        attach_features(store, run_id="run-1", foundation_dir=foundation)
        features = store.features_for("allstar", "f" * 24)
        assert features["headshot_rate"]["value_num"] is None
        assert features["headshot_rate"]["provenance"] == "unavailable"
        assert "missing" in features["headshot_rate"]["evidence"]
        # Only confirmed True flags count; the missing flag is not counted as False.
        assert features["headshots"]["value_num"] == 0.0
    finally:
        store.close()


def test_quarantined_alignment_is_linked_but_not_measured(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "c" * 24, quarantined=True)
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        _add(store, _post("allstar", "c" * 24))
        stats = link_posts(store, foundation_dir=foundation, run_id="run-1")
        assert stats["quarantined_links"] == 1
        assert ("allstar", "c" * 24) in store.quarantined_posts()
        attach_features(store, run_id="run-1", foundation_dir=foundation)
        features = store.features_for("allstar", "c" * 24)
        # Post-level context is still measured; foundation-derived evidence is not.
        assert features["kills"]["provenance"] == "unavailable"
        assert features["clip_span_seconds"]["provenance"] == "unavailable"
        assert features["kills"]["value_num"] is None
    finally:
        store.close()


def test_relinking_clears_stale_hypotheses_and_keeps_them_ambiguous(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "a" * 24, player="kyousuke", map_name="Inferno")
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        # A post whose caption mentions player + map but carries no stable id.
        _add(store, _post("youtube", "hyp1", caption="kyousuke 4K on Inferno"))
        _add(store, _post("youtube", "hyp2", caption="no identity at all"))
        first = link_posts(store, foundation_dir=foundation, run_id="r1")
        assert first["fuzzy"] == 1
        rows = [dict(r) for r in store.conn.execute("SELECT * FROM clip_identity_links")]
        assert rows and all(r["ambiguous"] == 1 for r in rows)
        assert rows[0]["confidence"] < 0.8  # never groups or trains
        assert first["unlinked"] == 1
        # A second pass replaces rather than accumulates.
        second = link_posts(store, foundation_dir=foundation, run_id="r2")
        assert second["stale_link_rows_removed"] == len(rows)
        assert store.conn.execute(
            "SELECT COUNT(*) FROM clip_identity_links").fetchone()[0] == len(rows)
    finally:
        store.close()


def test_foundation_index_reads_alignment_and_match(tmp_path):
    foundation = tmp_path / "foundation"
    _write_clip(foundation, "d" * 24, map_name="Ancient")
    index = build_foundation_index(foundation)
    assert index["d" * 24]["map"] == "Ancient"
    assert index["d" * 24]["match_id"] == "2389644"
    assert index["d" * 24]["round"] == 12
