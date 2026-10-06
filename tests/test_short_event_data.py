import json

import numpy as np
import pandas as pd

from cs2archive.shorts.event_data import EVENT_DATA_VERSION, kill_event_details, raw_event_table


def test_kill_feed_flags_preserve_true_false_and_unknown():
    details = kill_event_details({
        "headshot": np.bool_(True), "assistedflash": True, "attackerblind": False,
        "noscope": pd.NA, "thrusmoke": float("nan"), "attackerinair": 1,
        "penetrated": np.int64(2), "assister_steamid": np.uint64(76561198134401925),
        "distance": 124.5, "dmg_health": np.int32(32), "hitgroup": 1,
    })
    assert details["headshot"] is True
    assert details["assistedflash"] is True
    assert details["attackerblind"] is False
    assert details["noscope"] is None
    assert details["thrusmoke"] is None
    assert details["attackerinair"] is True
    assert details["penetrated"] == 2
    assert details["assister_steamid"] == "76561198134401925"
    assert details["distance"] == 124.5
    assert details["dmg_health"] == 32
    assert details["hitgroup"] == 1
    json.dumps(details, allow_nan=False)


def test_missing_assist_flag_is_not_guessed_from_assister_id():
    details = kill_event_details({"assister_steamid": "76561198134401925"})
    assert details["assistedflash"] is None
    assert details["assister_steamid"] == "76561198134401925"


def test_raw_event_table_keeps_unfiltered_rows_and_exact_identity():
    table = raw_event_table(pd.DataFrame([
        {"tick": 0, "user_steamid": np.uint64(76561198134401925), "weapon": "world", "headshot": False},
        {"tick": 100, "user_steamid": np.uint64(76561198134401925), "weapon": "knife", "headshot": pd.NA},
    ]))
    assert len(table["rows"]) == 2
    assert table["rows"][0]["tick"] == 0
    assert table["rows"][0]["user_steamid"] == "76561198134401925"
    assert table["rows"][1]["headshot"] is None
    json.dumps(table, allow_nan=False)


def test_float_identity_is_not_written_as_a_fabricated_steamid():
    assert kill_event_details({"assister_steamid": float(76561198134401925)})["assister_steamid"] is None


def test_action_cache_rebuilds_when_raw_event_schema_is_missing(tmp_path, monkeypatch):
    from cs2archive.highlights import build_action_timeline as module
    path = tmp_path / "action_timeline.json"
    path.write_text(json.dumps({"timeline_version": module.TIMELINE_VERSION}))
    calls = []
    def build(demo):
        calls.append(demo)
        return {"timeline_version": module.TIMELINE_VERSION, "event_data_version": EVENT_DATA_VERSION}
    monkeypatch.setattr(module, "build_action_timeline", build)
    assert module.ensure_action_timeline(tmp_path / "demo.dem", output=path) == path
    assert len(calls) == 1
    assert module.ensure_action_timeline(tmp_path / "demo.dem", output=path) == path
    assert len(calls) == 1
