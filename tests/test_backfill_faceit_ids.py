from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

import backfill_faceit_ids as bf


def test_extract_match_ids_from_room_url():
    text = (
        "Match: https://www.faceit.com/en/cs2/room/"
        "1-7e33777d-20de-4304-8980-dac55f25f752\n"
        "also https://faceit.com/cs2/room/"
        "1-7e33777d-20de-4304-8980-dac55f25f752"
    )
    assert bf.extract_match_ids(text) == [
        "1-7e33777d-20de-4304-8980-dac55f25f752",
    ]


def test_extract_match_ids_skips_non_uuid_rooms():
    text = "Match: https://www.faceit.com/en/cs2/room/team_Brain47 vs team_ayanokojii - mirage"
    assert bf.extract_match_ids(text) == []


def test_roster_from_match_reads_both_factions():
    payload = {
        "teams": {
            "faction1": {"roster": [
                {"player_id": "aaa", "nickname": "FalleN", "game_player_id": "111"},
            ]},
            "faction2": {"roster": [
                {"player_id": "bbb", "nickname": "yuurih", "game_player_id": "222"},
            ]},
        }
    }
    roster = bf.roster_from_match(payload)
    assert {(p["nickname"], p["steam_id"]) for p in roster} == {
        ("FalleN", "111"),
        ("yuurih", "222"),
    }


def test_pick_roster_player_prefers_steam_id():
    roster = [
        {"player_id": "wrong", "nickname": "max", "steam_id": "999"},
        {"player_id": "right", "nickname": "max", "steam_id": "111"},
    ]
    hit = bf._pick_roster_player(roster, "max", "111")
    assert hit["player_id"] == "right"
