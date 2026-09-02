"""Flick detector: snap vs tracking, stacked on cuts, not quickscope."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.flick import is_flick


def _held(n: int, yaw: float = 10.0, pitch: float = 5.0) -> tuple[list[float], list[float]]:
    return [yaw] * n, [pitch] * n


def test_still_crosshair_is_not_a_flick():
    yaw, pitch = _held(33)
    assert not is_flick(yaw, pitch)


def test_smooth_tracking_is_not_a_flick():
    yaw = [10.0 + i * 1.2 for i in range(33)]
    pitch = [5.0] * 33
    assert not is_flick(yaw, pitch)


def test_late_yaw_snap_is_a_flick():
    yaw, pitch = _held(33)
    # ~80 deg in the last 6 diffs (~90ms) after a still hold.
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (80.0 / 6)
    assert is_flick(yaw, pitch)


def test_tiny_correction_is_not_a_flick():
    yaw, pitch = _held(33)
    yaw[-1] = 14.0
    assert not is_flick(yaw, pitch)


def test_yaw_wrap_snap_is_a_flick():
    yaw = [170.0] * 33
    pitch = [5.0] * 33
    for i in range(1, 7):
        raw = 170.0 + (7 - i) * (80.0 / 6)
        yaw[-i] = ((raw + 180.0) % 360.0) - 180.0
    assert is_flick(yaw, pitch)


def _snap_yaw(n: int = 33) -> tuple[list[float], list[float]]:
    yaw, pitch = _held(n)
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (80.0 / 6)
    return yaw, pitch


def test_collect_flick_kills_flags_late_snap():
    import pandas as pd
    from shorts.build_short_timeline import _collect_flick_kills
    from shorts.flick import PRE_TICKS

    kill_tick = 2000
    aid = "76561198000000000"
    yaw, pitch = _snap_yaw(PRE_TICKS + 1)
    ticks = list(range(kill_tick - PRE_TICKS, kill_tick + 1))
    rows = [
        {"tick": t, "steamid": aid, "yaw": yaw[i], "pitch": pitch[i]}
        for i, t in enumerate(ticks)
    ]

    class Parser:
        def parse_ticks(self, fields, ticks=None, players=None):
            return pd.DataFrame(rows)

    deaths = pd.DataFrame([{
        "tick": kill_tick,
        "attacker_steamid": aid,
        "weapon": "awp",
    }])
    got = _collect_flick_kills(Parser(), deaths, first_freeze=None)
    assert (aid, kill_tick) in got


def test_collect_flick_kills_skips_tracking_and_knife():
    import pandas as pd
    from shorts.build_short_timeline import _collect_flick_kills
    from shorts.flick import PRE_TICKS

    kill_tick = 2000
    aid = "76561198000000000"
    yaw = [10.0 + i * 1.2 for i in range(PRE_TICKS + 1)]
    pitch = [5.0] * (PRE_TICKS + 1)
    ticks = list(range(kill_tick - PRE_TICKS, kill_tick + 1))
    rows = [
        {"tick": t, "steamid": aid, "yaw": yaw[i], "pitch": pitch[i]}
        for i, t in enumerate(ticks)
    ]

    class Parser:
        def parse_ticks(self, fields, ticks=None, players=None):
            return pd.DataFrame(rows)

    tracking = pd.DataFrame([{
        "tick": kill_tick,
        "attacker_steamid": aid,
        "weapon": "awp",
    }])
    assert _collect_flick_kills(Parser(), tracking, first_freeze=None) == set()

    knife = pd.DataFrame([{
        "tick": kill_tick,
        "attacker_steamid": aid,
        "weapon": "knife",
    }])
    assert _collect_flick_kills(Parser(), knife, first_freeze=None) == set()

