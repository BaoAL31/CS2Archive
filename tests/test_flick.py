"""Flick detector: snap vs tracking, stacked on cuts, not quickscope."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.flick import CONVERT_TICKS, is_flick, is_flick_converted


def _held(n: int, yaw: float = 10.0, pitch: float = 5.0) -> tuple[list[float], list[float]]:
    return [yaw] * n, [pitch] * n


def test_still_crosshair_is_not_a_flick():
    yaw, pitch = _held(33)
    assert not is_flick(yaw, pitch)


def test_smooth_tracking_is_not_a_flick():
    yaw = [10.0 + i * 1.2 for i in range(33)]
    pitch = [5.0] * 33
    assert not is_flick(yaw, pitch)


def test_fast_tracking_then_snap_is_still_a_flick():
    """Already moving hard does not cancel a real throw onto the target."""
    yaw = [10.0 + i * 4.0 for i in range(27)]
    for i in range(1, 7):
        yaw.append(yaw[-1] + (80.0 / 6))
    pitch = [5.0] * len(yaw)
    assert is_flick(yaw, pitch)


def test_late_yaw_snap_is_a_flick():
    yaw, pitch = _held(33)
    # ~80 deg in the last 6 diffs (~90ms) after a still hold.
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (80.0 / 6)
    assert is_flick(yaw, pitch)
    assert is_flick_converted(yaw, pitch)


def test_flick_converted_allows_kill_0_2s_after_snap():
    yaw, pitch = _held(33)
    hold = 8
    end = len(yaw) - 1 - hold
    for i in range(1, 7):
        yaw[end + 1 - i] = 10.0 + (7 - i) * (80.0 / 6)
    for i in range(end + 1, len(yaw)):
        yaw[i] = yaw[end]
    assert not is_flick(yaw, pitch)
    assert is_flick_converted(yaw, pitch)


def test_flick_converted_rejects_swing_then_separate_poke():
    """Wide unscoped swing, pause, then a 10° settle — not a flick onto the kill."""
    yaw, pitch = _held(33)
    end = len(yaw) - 1 - 13
    for i in range(1, 7):
        yaw[end + 1 - i] = 10.0 + (7 - i) * (80.0 / 6)
    held = yaw[end]
    for i in range(end + 1, len(yaw) - 6):
        yaw[i] = held
    for i in range(1, 7):
        yaw[-i] = held + (7 - i) * (10.0 / 6)
    assert not is_flick(yaw, pitch, awp=True)
    assert not is_flick_converted(yaw, pitch, awp=True)


def test_flick_converted_rejects_kill_later_than_0_2s():
    n = 48
    yaw, pitch = _held(n)
    hold = CONVERT_TICKS + 16
    end = n - 1 - hold
    for i in range(1, 7):
        yaw[end + 1 - i] = 10.0 + (7 - i) * (80.0 / 6)
    for i in range(end + 1, n):
        yaw[i] = yaw[end]
    assert not is_flick(yaw, pitch)
    assert not is_flick_converted(yaw, pitch)


def test_tiny_correction_is_not_a_flick():
    yaw, pitch = _held(33)
    yaw[-1] = 14.0
    assert not is_flick(yaw, pitch)
    assert not is_flick(yaw, pitch, awp=True)


def test_short_ak_nudge_is_not_a_flick():
    """m0NESY review clip: ~42° / ~440°/s — visible aim, not a flick."""
    yaw, pitch = _held(33)
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (42.0 / 6)
    assert not is_flick(yaw, pitch)


def test_small_snap_is_awp_flick_not_rifle():
    yaw, pitch = _held(33)
    # ~55 deg in ~90ms: under rifle 75°, over AWP 50°.
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (55.0 / 6)
    assert not is_flick(yaw, pitch)
    assert is_flick(yaw, pitch, awp=True)


def test_heavygod_scale_ak_is_not_a_flick():
    yaw, pitch = _held(33)
    end = len(yaw) - 1 - 11
    for i in range(1, 7):
        yaw[end + 1 - i] = 10.0 + (7 - i) * (55.0 / 6)
    for i in range(end + 1, len(yaw)):
        yaw[i] = yaw[end]
    assert not is_flick(yaw, pitch)
    assert not is_flick_converted(yaw, pitch)


def test_slaxz_scale_awp_is_not_a_flick():
    yaw, pitch = _held(33)
    end = len(yaw) - 1 - 13
    for i in range(1, 7):
        yaw[end + 1 - i] = 10.0 + (7 - i) * (39.0 / 6)
    for i in range(end + 1, len(yaw)):
        yaw[i] = yaw[end]
    assert not is_flick(yaw, pitch, awp=True)
    assert not is_flick_converted(yaw, pitch, awp=True)


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
    assert (aid, kill_tick) in _collect_flick_kills(Parser(), deaths, first_freeze=None)
    assert (aid, kill_tick) in _collect_flick_kills(
        Parser(), deaths, first_freeze=None, converted=True,
    )


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


def test_collect_uses_looser_awp_gates():
    import pandas as pd
    from shorts.build_short_timeline import _collect_flick_kills
    from shorts.flick import PRE_TICKS

    kill_tick = 2000
    aid = "76561198000000000"
    yaw, pitch = _held(PRE_TICKS + 1)
    for i in range(1, 7):
        yaw[-i] = 10.0 + (7 - i) * (55.0 / 6)
    ticks = list(range(kill_tick - PRE_TICKS, kill_tick + 1))
    rows = [
        {"tick": t, "steamid": aid, "yaw": yaw[i], "pitch": pitch[i]}
        for i, t in enumerate(ticks)
    ]

    class Parser:
        def parse_ticks(self, fields, ticks=None, players=None):
            return pd.DataFrame(rows)

    awp = pd.DataFrame([{
        "tick": kill_tick,
        "attacker_steamid": aid,
        "weapon": "awp",
    }])
    rifle = pd.DataFrame([{
        "tick": kill_tick,
        "attacker_steamid": aid,
        "weapon": "ak47",
    }])
    assert (aid, kill_tick) in _collect_flick_kills(Parser(), awp, first_freeze=None)
    assert _collect_flick_kills(Parser(), rifle, first_freeze=None) == set()

