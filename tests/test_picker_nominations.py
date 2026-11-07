"""Picker nominations: demo facts, payload trimming, compact output."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cs2archive.shorts import picker_nominations as pn

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
SID = "76561198000000001"


def _iso(delta_days: float = 0.0) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


def _timeline(map_name: str = "de_mirage", round_no: int = 5) -> dict:
    return {
        "map": map_name,
        "teams": {"2": [SID, "76561198000000002"], "3": ["76561198000000003"]},
        "rounds": [{
            "round": round_no,
            "winner_team": 2,
            "score_after": {"2": 4, "3": 1},
            "win_reason": "bomb_exploded",
            "stakes": {"buys": {"2": "full", "3": "eco"},
                       "equip_value": {"2": 20000, "3": 4000},
                       "equip_players": {"2": [SID]},
                       "is_pistol": False, "deficit_at_start": 0},
            "alive": [{"tick": 1, "teams": {"2": 5, "3": 5}}],
            "hot_zones": [{"zone": "A"}],
        }],
        "kills_all": [
            {"tick": 1000, "round": round_no, "attacker_steam_id": SID,
             "attacker": "donk", "victim": "ZywOo", "weapon": "ak47",
             "victim_weapon": "m4a1", "attacker_place": "Palace",
             "victim_place": "Connector",
             "kill_event": {"headshot": True, "hitgroup": "head", "dmg_health": 100,
                            "distance": 12.5, "thrusmoke": False, "penetrated": 0}},
            {"tick": 1064, "round": round_no, "attacker_steam_id": SID,
             "attacker": "donk", "victim": "apEX", "weapon": "ak47",
             "victim_weapon": "ak47", "attacker_place": "Palace",
             "victim_place": "Jungle",
             "kill_event": {"headshot": False, "hitgroup": "chest", "dmg_health": 87,
                            "distance": 20.0, "thrusmoke": True, "penetrated": 1}},
            {"tick": 1070, "round": round_no, "attacker_steam_id": "76561198000000002",
             "attacker": "sh1ro", "victim": "flameZ", "weapon": "awp",
             "kill_event": {"headshot": False}},
        ],
    }


def _clip(**over) -> dict:
    base = {
        "clip_id": "c1", "player": "donk", "steamid": SID, "opponent": "Vitality",
        "match_id": "2400001", "hltv_match": "https://www.hltv.org/matches/2400001",
        "event_slug": "iem-beijing-2026", "map": "mirage", "round": 5,
        "kinds": ["4k"], "title": "Mirage AK-47 4K", "label": "donk Mirage AK-47 4K",
        "stage": "playoff", "views": 5000, "views_per_hour": 100.0,
        "recent_views_per_hour": None, "snapshots": 1,
        "published_at": _iso(-2), "clip_length": 24.0, "score_line": "13 - 7",
        "series_game_label": "Game 1 of 3", "match_date": "2026-11-04",
        "age_days": 2.0, "clip_age_days": 2.0, "observed_at": _iso(),
    }
    base.update(over)
    return base


def _folder(tmp_path, name="2400001-a-vs-b-iem-beijing-2026"):
    folder = tmp_path / "demos" / name
    folder.mkdir(parents=True)
    (folder / "a-vs-b-m1-mirage.dem").touch()
    return folder


def test_facts_for_returns_none_when_the_demo_is_not_local():
    assert pn.facts_for(_clip(), None) is None


def test_facts_for_extracts_the_round_and_drops_unused_fields(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    monkeypatch.setattr(pn, "timeline_for", lambda demo, cache=None: _timeline())
    facts = pn.facts_for(_clip(), folder, cache=tmp_path / "cache")

    assert facts["map"] == "de_mirage"
    assert facts["round_won_by_player_team"] is True
    assert facts["player_kills_in_round"] == 2
    assert facts["all_kills_in_round"] == 3
    assert facts["headshots"] == 1
    assert facts["kill_span_seconds"] == 1.0
    assert facts["gaps_seconds"] == [1.0]
    assert facts["buy"] == {"2": "full", "3": "eco"}
    assert facts["kills"][0]["t_after_first_s"] == 0.0
    assert facts["kills"][1]["t_after_first_s"] == 1.0
    assert facts["kills"][1]["through_smoke"] is True
    assert [k["attacker"] for k in facts["other_kills_in_round"]] == ["sh1ro"]
    # The prompt never names these, and they were 10 KB of the payload.
    for dropped in ("alive_counts_over_round", "hot_zones", "equip_players",
                    "round_winner_team_id"):
        assert dropped not in facts


def test_facts_for_reports_ambiguity_instead_of_guessing(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    monkeypatch.setattr(pn, "timeline_for", lambda demo, cache=None: _timeline())
    facts = pn.facts_for(_clip(round=99), folder, cache=tmp_path / "cache")
    assert facts == {"error": "moment not found"}


def test_facts_for_pins_the_map_when_a_series_repeats_the_round(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    (folder / "a-vs-b-m2-inferno.dem").touch()

    def fake_timeline(demo, cache=None):
        return _timeline("de_inferno" if "inferno" in demo.name else "de_mirage")

    monkeypatch.setattr(pn, "timeline_for", fake_timeline)
    facts = pn.facts_for(_clip(map="inferno"), folder, cache=tmp_path / "cache")
    assert facts["map"] == "de_inferno"


def test_series_summary_reports_every_map_and_the_result(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    (folder / "a-vs-b-m2-inferno.dem").touch()

    def fake_timeline(demo, cache=None):
        return _timeline("de_inferno" if "inferno" in demo.name else "de_mirage")

    monkeypatch.setattr(pn, "timeline_for", fake_timeline)
    series = pn.series_summary(folder, "2", cache=tmp_path / "cache")
    assert [row["map"] for row in series] == ["de_mirage", "de_inferno"]
    assert all(row["player_team_won_map"] is True for row in series)
    assert series[0]["player_team_rounds"] == 4
    assert series[0]["opponent_rounds"] == 1


def test_write_nominations_splits_facts_into_their_own_files(tmp_path):
    payload = {
        "nominations": [
            {"clip_id": "c1", "views": 10, "facts": {"map": "de_mirage", "kills": []}},
            {"clip_id": "c2", "views": 5, "facts": None},
        ],
    }
    written = pn.write_nominations(payload, tmp_path / "n.json")
    index = json.loads((tmp_path / "n.json").read_text(encoding="utf-8"))

    assert written["facts_files"] == 1
    # The index must not carry the per-kill detail: it is what made the file
    # unreadable to the agent's read tool.
    assert "facts" not in index["nominations"][0]
    assert index["nominations"][0]["facts_file"].endswith("c1.json")
    assert "facts_file" not in index["nominations"][1]
    facts = json.loads((tmp_path / "facts" / "c1.json").read_text(encoding="utf-8"))
    assert facts["map"] == "de_mirage"


def test_write_nominations_keeps_every_line_under_the_read_tool_limit(tmp_path):
    """The agent's read tool refuses any line over 50 KB."""
    payload = {"nominations": [
        {"clip_id": f"c{i}", "facts": {"kills": [{"tick": i}] * 50}} for i in range(5)
    ]}
    pn.write_nominations(payload, tmp_path / "n.json")
    for path in [tmp_path / "n.json", *sorted((tmp_path / "facts").glob("*.json"))]:
        longest = max(len(line) for line in path.read_text(encoding="utf-8").splitlines())
        assert longest < 50_000, path


def test_build_nominations_attaches_facts_and_reports_the_pool(tmp_path, monkeypatch):
    probe = tmp_path / "probe.jsonl"
    probe.write_text(json.dumps({
        "match_id": "2400001", "slug": "a-vs-b-iem-beijing-2026",
        "event_slug": "iem-beijing-2026", "ok": True, "playlist_id": "p",
        "match_date": "2026-11-04", "scraped_at": _iso(),
        "clips": [{"source": "allstar", "clip_id": "c1", "steamid": SID,
                   "player": "donk", "match_id": "2400001", "kinds": ["4k"],
                   "title": "Mirage AK-47 4K", "round": 5, "views": 5000,
                   "published_at": _iso(-2), "observed_at": _iso()}],
    }) + "\n", encoding="utf-8")
    folder = _folder(tmp_path)
    monkeypatch.setattr(pn, "timeline_for", lambda demo, cache=None: _timeline())

    payload = pn.build_nominations(
        7, 25, out=probe, snapshots=tmp_path / "snaps.jsonl",
        db=tmp_path / "absent.sqlite3", cache=tmp_path / "cache",
        demos_root=tmp_path / "demos", now=NOW,
    )
    assert payload["total_eligible"] == 1
    assert payload["shown"] == 1
    assert payload["with_demo_facts"] == 1
    nomination = payload["nominations"][0]
    assert nomination["local_demo_folder"] == folder.name
    assert nomination["facts"]["map"] == "de_mirage"
    assert nomination["facts_summary"]["headshots"] == 1
    assert nomination["facts_summary"]["weapons"] == ["ak47"]
    assert nomination["facts_summary"]["wallbangs"] == 1
    assert nomination["facts_summary"]["series"][0]["map"] == "de_mirage"
    assert nomination["views_per_hour"] == round(5000 / 48.0, 2)
    assert payload["window"]["oldest_match_date"] == "2026-11-04"


def test_default_shortlist_is_the_five_most_viewed(tmp_path, monkeypatch):
    """One Short per day, scrape every 2 days: five candidates, one decision."""
    probe = tmp_path / "probe.jsonl"
    clips = [
        {"source": "allstar", "clip_id": f"c{i}", "steamid": SID, "player": "donk",
         "match_id": "2400001", "kinds": ["4k"], "title": "Mirage AK-47 4K",
         "round": 5, "views": views, "published_at": _iso(-2), "observed_at": _iso()}
        for i, views in enumerate([100, 9000, 300, 7000, 5000, 8000, 200, 6000])
    ]
    probe.write_text(json.dumps({
        "match_id": "2400001", "slug": "a-vs-b-iem-beijing-2026",
        "event_slug": "iem-beijing-2026", "ok": True, "playlist_id": "p",
        "match_date": "2026-11-04", "scraped_at": _iso(), "clips": clips,
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(pn, "timeline_for", lambda demo, cache=None: _timeline())

    payload = pn.build_nominations(
        7, out=probe, snapshots=tmp_path / "snaps.jsonl",
        db=tmp_path / "absent.sqlite3", cache=tmp_path / "cache",
        demos_root=tmp_path / "demos", now=NOW,
    )
    assert payload["total_eligible"] == 8
    assert payload["shown"] == 5
    assert [n["views"] for n in payload["nominations"]] == [9000, 8000, 7000, 6000, 5000]
