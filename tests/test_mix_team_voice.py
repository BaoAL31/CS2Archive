from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

from mix_team_voice import _as_steamid, teammate_voice_map


def test_as_steamid_normalizes_float_pandas_ids():
    assert _as_steamid(76561198034202275) == "76561198034202275"
    assert _as_steamid("76561198034202275.0") == "76561198034202275"
    assert _as_steamid(None) == ""


def test_teammate_voice_map_drops_the_other_team():
    pov = "76561198034202275"
    players = [
        (pov, 3),
        ("111", 3),
        ("222", 3),
        ("333", 3),
        ("444", 3),
        ("555", 2),
        ("666", 2),
        ("777", 2),
        ("888", 2),
        ("999", 2),
    ]
    kept = teammate_voice_map(players, pov)
    assert set(kept) == {pov, "111", "222", "333", "444"}
    assert "555" not in kept
    assert "999" not in kept
