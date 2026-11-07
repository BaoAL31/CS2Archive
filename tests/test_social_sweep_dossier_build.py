"""Dossier build pass: enumeration, bounded idempotency, honest failure reporting."""

from __future__ import annotations

import json

import pandas as pd

from cs2archive.shorts.social.dossier import moment_id
from cs2archive.shorts.social.dossier_build import (
    build_all, enrich_state_batch, iter_linkage_moments, iter_moments,
    plan_state_enrichment, timeline_path_for,
)

STEAM = "76561199032006224"
DEMO = "demos/hltv/2399649-a-vs-b-iem-krakw/a-vs-b-m3-inferno.dem"


def _foundation(tmp_path, *, clips=2, with_state=True, quarantined=False):
    foundation = tmp_path / "shorts-foundation"
    (foundation / "_timelines").mkdir(parents=True)
    timeline = {
        "timeline_version": 3, "event_data_version": 1,
        "demo_path": DEMO, "map": "Inferno",
        "teams": {"2": [STEAM]},
        "raw_player_info": {"rows": [{"steamid": STEAM, "name": "kyousuke",
                                      "team_number": 2}]},
        "sides_by_round": {"12": {"T": 2, "CT": 3}},
        "rounds": [{"round": 12, "start_tick": 80000, "end_tick": 87600,
                    "winner_team": 2, "win_reason": "t_killed",
                    "alive": [{"tick": 80000, "teams": {"2": 3, "3": 4}}]}],
        "kills_all": [{"tick": 86559, "round": 12, "attacker": "kyousuke",
                       "attacker_steam_id": STEAM, "victim": "m0NESY",
                       "victim_steam_id": "76561198074762801", "weapon": "ak47",
                       "headshot": True, "kill_event": {"headshot": True}}],
        "raw_events": {"player_hurt": {"rows": []}, "weapon_fire": {"rows": []}},
        "util_throws": [], "util_damages": [], "bomb_actions": [],
    }
    (foundation / "_timelines" / "2399649-a-vs-b-iem-krakw__a-vs-b-m3-inferno.json"
     ).write_text(json.dumps(timeline), encoding="utf-8")

    for i in range(clips):
        clip = foundation / "clips" / f"clip{i}"
        clip.mkdir(parents=True)
        (clip / "alignment.json").write_text(json.dumps({
            "clip_id": f"clip{i}", "demo": DEMO, "match_id": "2399649",
            "steamid": STEAM, "player": "kyousuke", "round": 12,
            "tick_start": 86239, "tick_end": 86862,
            "round_tick_start": 80000, "round_tick_end": 87600,
            "kill_ticks": [86559], "confidence": "exact",
            "method": "steamid+map+round+kills",
            "quarantined": quarantined}), encoding="utf-8")
        pd.DataFrame([{"key": "kills", "payload": json.dumps(
            timeline["kills_all"])}]).to_parquet(clip / "events.parquet", index=False)
        if with_state:
            pd.DataFrame([{"tick": 86240, "steamid": STEAM, "X": 1.0,
                           "health": 100}]).to_parquet(clip / "state.parquet",
                                                       index=False)
    return foundation


def test_timeline_path_derivation_matches_the_cached_naming(tmp_path):
    foundation = _foundation(tmp_path)
    path = timeline_path_for(DEMO, foundation / "_timelines")
    assert path is not None and path.name == \
        "2399649-a-vs-b-iem-krakw__a-vs-b-m3-inferno.json"
    # Windows-style demo paths in the staged alignment must resolve too.
    assert timeline_path_for(DEMO.replace("/", "\\"),
                             foundation / "_timelines") is not None
    assert timeline_path_for("demos/hltv/other/other-m1-nuke.dem",
                             foundation / "_timelines") is None
    assert timeline_path_for(None, foundation / "_timelines") is None


def test_iter_moments_skips_quarantined_alignments(tmp_path):
    foundation = _foundation(tmp_path, clips=2, quarantined=True)
    assert iter_moments(foundation) == []
    assert len(iter_moments(foundation, skip_quarantined=False)) == 2
    assert len(iter_moments(foundation, skip_quarantined=False, limit=1)) == 1


def test_two_clips_of_the_same_moment_share_one_dossier(tmp_path):
    foundation = _foundation(tmp_path, clips=2)  # identical windows on purpose
    root = tmp_path / "dossiers"
    stats = build_all(foundation_dir=foundation, root=root, log=lambda *_: None)
    assert stats["built"] == 1 and stats["failed"] == 0
    dossier = json.loads(next(p for p in root.glob("*.json")
                              if p.name != "INDEX.json").read_text(encoding="utf-8"))
    # Both evidence paths are preserved on the single moment document.
    assert len(dossier["staged_clips"]) == 2
    assert sorted(dossier["clip_verification"]["allstar_clip_ids"]) ==         ["clip0", "clip1"]


def test_build_all_writes_dossiers_and_index_then_skips_current(tmp_path):
    foundation = _foundation(tmp_path, clips=2)
    for offset, clip in enumerate(("clip0", "clip1")):
        path = foundation / "clips" / clip / "alignment.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["kill_ticks"] = [86559 + offset * 100]
        payload["tick_end"] = 86862 + offset
        path.write_text(json.dumps(payload), encoding="utf-8")
    root = tmp_path / "dossiers"
    first = build_all(foundation_dir=foundation, root=root, log=lambda *_: None)
    assert first["built"] == 2 and first["failed"] == 0
    assert (root / "INDEX.json").is_file()
    index = json.loads((root / "INDEX.json").read_text(encoding="utf-8"))
    assert index["availability"]["dossiers"] == 2
    assert index["availability"]["present_share"]["chronology"] == 1.0
    # A dossier is a stable file with both renderings.
    names = sorted(p.stem for p in root.glob("*.json") if p.name != "INDEX.json")
    assert all((root / f"{n}.txt").is_file() for n in names)

    second = build_all(foundation_dir=foundation, root=root, log=lambda *_: None)
    assert second["built"] == 0 and second["skipped_current"] == 2
    forced = build_all(foundation_dir=foundation, root=root, force=True,
                       log=lambda *_: None)
    assert forced["built"] == 2


def test_build_all_isolates_failures_and_keeps_going(tmp_path):
    foundation = _foundation(tmp_path, clips=2)
    bad = foundation / "clips" / "clip0" / "alignment.json"
    bad.write_text("{not json", encoding="utf-8")
    root = tmp_path / "dossiers"
    stats = build_all(foundation_dir=foundation, root=root, log=lambda *_: None)
    # The unreadable alignment is simply not enumerated; the good one still builds.
    assert stats["built"] == 1 and stats["failed"] == 0

    # A dossier that raises is recorded, not fatal.
    (foundation / "clips" / "clip1" / "alignment.json").write_text(json.dumps({
        "clip_id": "clip1", "demo": DEMO, "steamid": STEAM, "round": 12,
        "tick_start": "not-a-tick", "tick_end": 86862, "kill_ticks": [86559]}),
        encoding="utf-8")
    stats = build_all(foundation_dir=foundation, root=root,
                      force=True, log=lambda *_: None)
    assert stats["failed"] == 1
    assert stats["failures"][0]["error"].startswith("ValueError")


def test_plan_state_enrichment_only_lists_moments_without_state(tmp_path):
    with_state = _foundation(tmp_path / "a", clips=1, with_state=True)
    assert plan_state_enrichment(foundation_dir=with_state) == []
    without = _foundation(tmp_path / "b", clips=2, with_state=False)
    todo = plan_state_enrichment(foundation_dir=without)
    assert len(todo) == 2
    assert all(t["tick_start"] == 86239 for t in todo)
    assert len(plan_state_enrichment(foundation_dir=without, limit=1)) == 1
    # An already-enriched moment is not planned again.
    (without / "clips" / "clip0" / "state_enriched.parquet").write_bytes(b"")
    assert len(plan_state_enrichment(foundation_dir=without)) == 1


def test_enrich_state_batch_reports_missing_demos_without_raising(tmp_path):
    foundation = _foundation(tmp_path, clips=2, with_state=False)
    stats = enrich_state_batch(foundation_dir=foundation, limit=2,
                              log=lambda *_: None)
    assert stats["candidates"] == 2
    assert stats["enriched"] == 0
    # A demo that is no longer on disk is a known limitation, not an error.
    assert stats["moments_without_demo"] == 2
    assert stats["demos_unavailable"] == 1
    assert stats["failures"] == []
    assert not list(foundation.glob("clips/*/state_enriched.parquet"))


def test_linkage_moments_parse_json_text_columns(tmp_path):
    from cs2archive.shorts.social.store import SocialStore

    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_short_linkage({
            "short_id": "s1", "platform": "youtube", "post_id": "v1",
            "demo_path": DEMO, "round": 12, "kill_ticks": [86559, 86641],
            "player_steamid": STEAM}, status="pending", run_id="r")
        store.upsert_short_linkage({
            "short_id": "s2", "demo_path": DEMO, "round": None}, status="pending",
            run_id="r")
        moments = iter_linkage_moments(store)
        assert len(moments) == 1                     # round-less rows are skipped
        record = moments[0]["record"]
        assert record["kill_ticks"] == [86559, 86641]  # parsed from JSON text
        assert isinstance(record["selection"], dict)
    finally:
        store.close()


def test_build_all_includes_linkage_moments_with_the_ledger_source(tmp_path):
    from cs2archive.shorts.social.store import SocialStore

    foundation = _foundation(tmp_path, clips=1)
    store = SocialStore(tmp_path / "db.sqlite3")
    try:
        store.upsert_short_linkage({
            "short_id": "led1", "demo_path": DEMO, "round": 12,
            "kill_ticks": [86559], "player_steamid": STEAM,
            "player_nick": "kyousuke"}, status="pending", run_id="r")
        root = tmp_path / "dossiers"
        stats = build_all(foundation_dir=foundation, root=root, store=store,
                          log=lambda *_: None)
        assert stats["by_source"].get("linkage_pending") == 1
        assert stats["built"] == 2
        docs = [json.loads(p.read_text(encoding="utf-8"))
                for p in root.glob("*.json") if p.name != "INDEX.json"]
        ledger = [d for d in docs if d["source"] == "linkage_pending"]
        assert len(ledger) == 1
        assert ledger[0]["linkage_record"]["status"] == "pending"
        assert ledger[0]["clip_verification"]["verified"] is False
        assert moment_id(ledger[0]) == ledger[0]["moment_id"]
    finally:
        store.close()
