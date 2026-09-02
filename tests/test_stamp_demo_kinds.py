"""Demo-kind stamps join Allstar clips by steam64 + round."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.stamp_demo_kinds import kinds_by_player_round, kinds_for_clip, stamp_joined_clips
from shorts.fit_partial_stars import apply_demo_kind_stamps


def test_kinds_by_player_round_merges_cuts_in_the_same_round():
    timeline = {
        "shorts": [
            {
                "short_type": "clutch",
                "pov_steam_id": "A",
                "round": 4,
                "clutch_initial_count": "1v3",
                "kill_ticks": [1, 2, 3, 4, 5],
            },
            {
                "short_type": "flick",
                "pov_steam_id": "A",
                "round": 4,
                "kill_ticks": [3],
                "flick": True,
            },
            {
                "short_type": "defuse",
                "pov_steam_id": "A",
                "round": 7,
                "kill_ticks": [],
            },
        ]
    }
    by = kinds_by_player_round(timeline)
    assert by[("A", 4)] >= {"1v3_won", "ace", "flick"}
    assert by[("A", 7)] == {"defuse"}


def test_kinds_for_clip_uses_steamid_and_round():
    parsed = {
        "demos/a.dem": {"A|4": ["flick", "ace"]},
        "demos/a-p2.dem": {"A|20": ["defuse"]},
    }
    clip = {
        "steamid": "A",
        "round": 4,
        "demo_path": "demos/a.dem",
        "demo_paths": ["demos/a.dem", "demos/a-p2.dem"],
    }
    assert kinds_for_clip(clip, parsed) == ["ace", "flick"]
    clip["round"] = 20
    assert kinds_for_clip(clip, parsed) == ["defuse"]


def test_stamp_joined_clips_keys_by_clip_id(tmp_path: Path):
    join = tmp_path / "join.json"
    join.write_text(
        json.dumps({
            "clips": [
                {
                    "status": "joined",
                    "clip_id": "c1",
                    "steamid": "A",
                    "round": 4,
                    "demo_path": "demos/a.dem",
                    "demo_paths": ["demos/a.dem"],
                },
                {"status": "no_match_demo", "clip_id": "c2", "steamid": "A", "round": 4},
            ]
        }),
        encoding="utf-8",
    )
    stamps = stamp_joined_clips(
        join_path=join,
        parsed={"demos/a.dem": {"A|4": ["flick"]}},
    )
    assert stamps == {"c1": ["flick"]}


def test_apply_demo_kind_stamps_merges_onto_label_kinds():
    rows = [
        {"clip_id": "c1", "kinds": ("4k",), "views": 10},
        {"clip_id": "c2", "kinds": ("3k",), "views": 10},
    ]
    out = apply_demo_kind_stamps(rows, {"c1": ["flick", "1v5_won"]})
    assert out[0]["kinds"] == ("4k", "1v5_won", "flick")
    assert out[1]["kinds"] == ("3k",)
