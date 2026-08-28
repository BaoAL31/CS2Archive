"""Pick the thumbnail frame where the POV killfeed is fullest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from thumbnail.utils import (
    KILLFEED_AFTER_SECONDS,
    KILLFEED_POV_SECONDS,
    demo_tick_to_video_seconds,
    killfeed_chain_start_tick,
    rank_killfeed_kills,
)

SID = "76561198000000000"
RATE = 64
WINDOW = int(round(RATE * KILLFEED_POV_SECONDS))  # 480


def _kills(*ticks: int, sid: str = SID) -> list[dict]:
    return [{"killerSteamId": sid, "tick": t, "weaponName": "ak47"} for t in ticks]


def test_empty_when_no_pov_kills():
    assert rank_killfeed_kills(_kills(100, sid="other"), SID, RATE) == []


def test_cluster_beats_later_single():
    # Three POV kills inside 7.5s, then an isolated kill much later.
    ranked = rank_killfeed_kills(_kills(1000, 1100, 1200, 9000), SID, RATE)
    assert ranked[0][0]["tick"] == 1200
    assert ranked[0][1] == 3


def test_capture_is_last_kill_of_window():
    ranked = rank_killfeed_kills(_kills(100, 200, 300), SID, RATE)
    assert ranked[0][0]["tick"] == 300
    assert ranked[0][1] == 3


def test_kills_just_inside_window_count():
    ranked = rank_killfeed_kills(_kills(0, WINDOW), SID, RATE)
    assert ranked[0][1] == 2
    assert ranked[0][0]["tick"] == WINDOW


def test_kills_just_outside_window_split():
    ranked = rank_killfeed_kills(_kills(0, WINDOW + 1), SID, RATE)
    assert ranked[0][1] == 1
    assert ranked[0][0]["tick"] == WINDOW + 1


def test_later_equal_cluster_wins_tie():
    ranked = rank_killfeed_kills(_kills(100, 200, 5000, 5100), SID, RATE)
    assert ranked[0][0]["tick"] == 5100
    assert ranked[0][1] == 2


def test_steam_id_int_vs_str():
    kills = [{"killerSteamId": int(SID), "tick": 50}]
    ranked = rank_killfeed_kills(kills, SID, RATE)
    assert ranked[0][0]["tick"] == 50
    assert ranked[0][1] == 1


def test_attacker_steam_id_from_shorts_timeline():
    kills = [
        {"attacker_steam_id": SID, "tick": 100},
        {"attacker_steam_id": SID, "tick": 200},
        {"attacker_steam_id": SID, "tick": 300},
    ]
    ranked = rank_killfeed_kills(kills, SID, RATE)
    assert ranked[0][0]["tick"] == 300
    assert ranked[0][1] == 3


def test_persist_action_timeline_writes_kills(tmp_path):
    from shorts.build_short_timeline import persist_action_timeline

    path = persist_action_timeline(
        tmp_path / "match.dem",
        {"demo_path": "demos/faceit/match.dem", "map": "de_dust2",
         "tickrate": 64, "kills": [{"tick": 10, "attacker_steam_id": SID}]},
        output_dir=tmp_path,
    )
    assert path == tmp_path / "action_timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["kills"][0]["tick"] == 10


def test_chain_start_is_first_kill_still_on_feed():
    assert killfeed_chain_start_tick(_kills(1000, 1100, 1200, 9000), SID, 1200, RATE) == 1000
    assert killfeed_chain_start_tick(_kills(1000, 1100, 1200, 9000), SID, 9000, RATE) == 9000


def test_demo_tick_to_video_seconds_linear_in_round():
    sidecar = {
        "round_offsets": {"1": 0.0, "2": 10.0},
        "per_round_ticks": {"1": [1000, 2000], "2": [3000, 4000]},
        "per_round_durations": {"1": 10.0, "2": 8.0},
        "total_duration_seconds": 18.0,
    }
    assert demo_tick_to_video_seconds(sidecar, 1500) == 5.0
    assert demo_tick_to_video_seconds(sidecar, 3500) == 14.0
    assert demo_tick_to_video_seconds(sidecar, 2500) is None


def test_killfeed_after_seconds_is_settle():
    assert KILLFEED_AFTER_SECONDS == 0.4
