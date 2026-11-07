"""Deepening: stage extra clips only from exact cached-timeline evidence."""

from __future__ import annotations

import json

import pandas as pd

from cs2archive.shorts.social.deepen import expected_kills, stage_additional_clips

STEAM = "76561199032006224"


def _timeline(path, *, kills, steamid=STEAM, round_no=1, map_name="Inferno",
              demo_path="demos/x.dem"):
    payload = {
        "timeline_version": 3, "map": map_name, "demo_path": demo_path,
        "rounds": [{"round": round_no, "start_tick": 1000, "end_tick": 9000}],
        "kills_all": kills,
        "raw_events": {
            "player_hurt": {"rows": [
                {"tick": 5000, "attacker_steam_id": steamid, "weapon": "ak47",
                 "dmg_health": 40},
                {"tick": 99000, "attacker_steam_id": steamid, "weapon": "ak47",
                 "dmg_health": 40},
            ]},
            "weapon_fire": {"rows": [
                {"tick": 4990, "user_steamid": steamid, "weapon": "weapon_ak47"},
                {"tick": 99500, "user_steamid": steamid, "weapon": "weapon_ak47"},
            ]},
            "player_death": {"rows": [
                {"tick": 5000, "attacker_steam_id": steamid, "user_steamid": "1"},
            ]},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _store(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _clip(clip_id, *, title, kinds=None, round_no=1):
    return {"clip_id": clip_id, "steamid": STEAM, "player": "p", "title": title,
            "kinds": kinds or [], "match_id": "1", "round": round_no, "views": 10,
            "observed_at": "2026-10-02T02:00:00+00:00"}


def test_expected_kills_reads_title_then_kinds():
    assert expected_kills({"title": "ACE on Mirage"}) == 5
    assert expected_kills({"title": "clean 4K"}) == 4
    assert expected_kills({"title": "clutch", "kinds": ["3k"]}) == 3
    assert expected_kills({"title": "clutch", "kinds": ["1v3_won"]}) is None


def test_only_exact_kill_matches_are_staged(tmp_path):
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [
        {"tick": 5000, "round": 1, "attacker_steam_id": STEAM},
        {"tick": 7000, "round": 1, "attacker_steam_id": STEAM},
    ]
    _timeline(foundation / "_timelines" / "1-a-vs-b-m1-inferno.json", kills=kills)
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "1", "scraped_at": "2026-10-02T02:00:00+00:00",
                    "clips": [
                        _clip("exact1", title="2K on Inferno"),
                        _clip("wrong_count", title="4K on Inferno"),
                        _clip("no_explicit", title="nice clutch", kinds=["1v3_won"]),
                    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 1
    assert stats.rejected_no_exact == 1
    assert stats.rejected_no_expected == 1
    clip_dir = foundation / "clips" / "exact1"
    alignment = json.loads((clip_dir / "alignment.json").read_text(encoding="utf-8"))
    assert alignment["confidence"] == "exact"
    assert alignment["explicit_kills"] == 2 and alignment["demo_kills"] == 2
    assert alignment["kill_ticks"] == [5000, 7000]
    assert alignment["state_missing"] is True
    assert (alignment["tick_start"], alignment["tick_end"]) == (4680, 7128)
    events = pd.read_parquet(clip_dir / "events.parquet")
    hurts = json.loads(events[events["key"] == "player_hurt"].iloc[0]["payload"])
    assert [row["tick"] for row in hurts] == [5000]  # out-of-window row dropped
    # Repeat run is idempotent: nothing new, existing counted.
    second = stage_additional_clips(store, foundation, max_new=10)
    assert second.staged == 0
    assert second.skipped_existing >= 1
    cached = json.loads((clip_dir / "alignment.json").read_text(encoding="utf-8"))
    assert cached["kill_ticks"] == [5000, 7000]


def test_wrong_round_is_rejected_not_staged(tmp_path):
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM}]
    _timeline(foundation / "_timelines" / "1-a-vs-b-m1-inferno.json", kills=kills)
    clip = _clip("wronground", title="Inferno 1K")
    clip["round"] = 9
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "1", "clips": [clip]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 0
    assert stats.rejected_no_round == 1
    assert not (foundation / "clips" / "wronground").exists()


def test_title_map_must_agree_with_the_timeline_map(tmp_path):
    """A same-round/same-kill-count round on the wrong map must never be staged."""
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [
        {"tick": 5000, "round": 1, "attacker_steam_id": STEAM},
        {"tick": 7000, "round": 1, "attacker_steam_id": STEAM},
    ]
    # Series part on Mirage only; the clip title claims Dust 2 with 2 kills.
    _timeline(foundation / "_timelines" / "1-a-vs-b-m1-mirage.json", kills=kills,
              map_name="Mirage")
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "1", "clips": [
        _clip("wrongmap", title="Dust 2 2K"),
        _clip("rightmap", title="Mirage 2K"),
    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 1
    assert stats.rejected_map_mismatch == 1
    assert (foundation / "clips" / "rightmap").exists()
    assert not (foundation / "clips" / "wrongmap").exists()
    align = json.loads(
        (foundation / "clips" / "rightmap" / "alignment.json").read_text(encoding="utf-8"))
    assert align["map_verified"] is True
    assert align["title_map"] == "mirage" and align["timeline_map"] == "Mirage"


def test_dust2_aliases_and_de_prefix_are_normalised(tmp_path):
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM}]
    _timeline(foundation / "_timelines" / "1-a-vs-b-m1-dust2.json", kills=kills,
              map_name="de_dust2")
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "1", "clips": [
        _clip("alias1", title="Dust 2 M4A4 1K"),
        _clip("alias2", title="dust_2 1K"),
    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 2
    assert stats.rejected_map_mismatch == 0


def test_title_without_a_map_is_never_exact(tmp_path):
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM}]
    _timeline(foundation / "_timelines" / "1-a-vs-b-m1-inferno.json", kills=kills)
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "1", "clips": [
        _clip("nomap", title="1K Clutch", kinds=["1k"]),
    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 0
    assert stats.rejected_no_map == 1
    assert not (foundation / "clips" / "nomap").exists()


def test_split_demo_ambiguity_is_rejected_not_silently_resolved(tmp_path):
    """p1/p2 restart rounds, so the same round+kills on two parts is ambiguous."""
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    kills = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM}]
    _timeline(foundation / "_timelines" / "7-a-vs-b-m1-inferno-p1.json", kills=kills,
              demo_path="demos/m1-p1.dem")
    _timeline(foundation / "_timelines" / "7-a-vs-b-m1-inferno-p2.json", kills=kills,
              demo_path="demos/m1-p2.dem")
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "7", "clips": [
        {"clip_id": "ambig", "steamid": STEAM, "player": "p", "title": "Inferno 1K",
         "kinds": [], "match_id": "7", "round": 1, "views": 10},
    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 0
    assert stats.rejected_ambiguous == 1
    assert not (foundation / "clips" / "ambig").exists()


def test_same_map_two_parts_with_different_kills_resolves_unambiguously(tmp_path):
    foundation = tmp_path / "foundation"
    (foundation / "_timelines").mkdir(parents=True)
    (foundation / "clips").mkdir()
    one = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM}]
    two = [{"tick": 5000, "round": 1, "attacker_steam_id": STEAM},
           {"tick": 6000, "round": 1, "attacker_steam_id": STEAM}]
    _timeline(foundation / "_timelines" / "7-a-vs-b-m1-inferno-p1.json", kills=one,
              demo_path="demos/m1-p1.dem")
    _timeline(foundation / "_timelines" / "7-a-vs-b-m1-inferno-p2.json", kills=two,
              demo_path="demos/m1-p2.dem")
    store = tmp_path / "store.jsonl"
    _store(store, [{"match_id": "7", "clips": [
        {"clip_id": "unambig", "steamid": STEAM, "player": "p", "title": "Inferno 2K",
         "kinds": [], "match_id": "7", "round": 1, "views": 10},
    ]}])
    stats = stage_additional_clips(store, foundation, max_new=10)
    assert stats.staged == 1
    assert stats.rejected_ambiguous == 0
    align = json.loads(
        (foundation / "clips" / "unambig" / "alignment.json").read_text(encoding="utf-8"))
    assert align["demo"].endswith("m1-p2.dem")
    assert align["demo_kills"] == 2
