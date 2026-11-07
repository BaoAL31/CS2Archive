import json

from cs2archive.shorts.build_foundation import (
    _is_quarantined, recut_staged, short_type_for_kinds,
)


def test_short_type_mapping_prefers_clutch_then_multi():
    assert short_type_for_kinds(["1v3_won", "3k"]) == ("clutch", "1v3")
    assert short_type_for_kinds(["ace", "wallbang"]) == ("4k", None)
    assert short_type_for_kinds(["wallbang"]) == ("wallbang", None)
    assert short_type_for_kinds(["3k"]) == ("3k", None)
    assert short_type_for_kinds([]) == ("highlight", None)


def test_is_quarantined_flags_and_missing_is_open(tmp_path):
    clip = tmp_path / "clips" / "abc"
    clip.mkdir(parents=True)
    assert _is_quarantined(tmp_path, "abc") is False
    assert _is_quarantined(tmp_path, "missing") is False
    (clip / "alignment.json").write_text(json.dumps({"quarantined": True}))
    assert _is_quarantined(tmp_path, "abc") is True


def test_recut_records_both_spans_and_filters_to_kills(tmp_path):
    import pandas as pd

    clip = tmp_path / "clips" / "abc"
    clip.mkdir(parents=True)
    (clip / "alignment.json").write_text(json.dumps({
        "demo": str(tmp_path / "m1" / "demo.dem"), "steamid": "1",
        "round": 2, "tick_start": 1000, "tick_end": 9000,
    }))
    tl_dir = tmp_path / "_timelines"
    tl_dir.mkdir()
    (tl_dir / "m1__demo.json").write_text(json.dumps({
        "kills_all": [
            {"attacker_steam_id": "1", "round": 2, "tick": 4000},
            {"attacker_steam_id": "1", "round": 2, "tick": 4200},
        ],
    }))
    pd.DataFrame([{"tick": t} for t in (1000, 3900, 4100, 4300, 9000)]
                 ).to_parquet(clip / "state.parquet", index=False)
    pd.DataFrame([{"key": "player_death",
                   "payload": json.dumps([{"tick": 1000}, {"tick": 4100}])}]
                 ).to_parquet(clip / "events.parquet", index=False)
    stats = recut_staged(tmp_path)
    assert stats == {"recut": 1, "skipped": 0, "errors": []}
    align = json.loads((clip / "alignment.json").read_text(encoding="utf-8"))
    assert (align["tick_start"], align["tick_end"]) == (3680, 4328)
    assert (align["round_tick_start"], align["round_tick_end"]) == (1000, 9000)
    assert align["kill_ticks"] == [4000, 4200]
    state = pd.read_parquet(clip / "state.parquet")
    assert state["tick"].min() >= 3680 and state["tick"].max() <= 4328


def test_rebuild_dedupes_clips_and_derives_match(tmp_path, monkeypatch):
    import cs2archive.shorts.rebuild_allstar_store as rebuild

    store = tmp_path / "probe.jsonl"
    store.write_text("\n".join(json.dumps(r) for r in [
        {"match_id": "1", "slug": "a-vs-b-esl-pro-league-season-24",
         "url": "u1", "ok": True, "playlist_id": "p1",
         "scraped_at": "2026-10-01T00:00:00+00:00",
         "clips": [
             {"clip_id": "c1", "match_id": "1", "views": 10, "title": "4K"},
             {"clip_id": "c2", "match_id": "2", "views": 50, "title": "ACE"},
         ]},
        {"match_id": "1", "slug": "a-vs-b-esl-pro-league-season-24",
         "url": "u1", "ok": True, "playlist_id": "p1",
         "scraped_at": "2026-10-02T00:00:00+00:00",
         "clips": [
             {"clip_id": "c1", "match_id": "1", "views": 30, "title": "4K"},
         ]},
        {"match_id": "3", "ok": False, "clips": []},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(rebuild, "STORE", store)
    assert rebuild.main([]) == 0
    rows = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    by_id = {r["match_id"]: r for r in rows}
    assert set(by_id) == {"1", "2"}
    assert by_id["1"]["clips"][0]["views"] == 30  # max-views snapshot wins
    assert by_id["1"]["clips"][0]["observed_at"] == "2026-10-02T00:00:00+00:00"
    assert by_id["2"]["clips"][0]["clip_id"] == "c2"  # own match, not row 1
    assert by_id["1"]["event_slug"] == "esl-pro-league-season-24"
    assert by_id["2"]["event_slug"] is None  # foreign row slug never inherited
