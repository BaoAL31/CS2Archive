"""Flick: a fast snap onto the target in the half-second before the kill.

Not quickscope. Quickscope is out of the kind set.
A flick is the throw itself (peak speed + yaw travel). Tracking first does
not disqualify it — you can already be moving and still flick.
"""
from __future__ import annotations

import math

TICKRATE = 64.0
PRE_TICKS = 32  # 0.5s lookback
SNAP_DIFFS = 8  # ~125ms at the kill

# Fast throw in the last ~125ms. 55° still looked like a nudge (HeavyGod AK
# review); 32° AWP same (slaxz-).
MIN_PEAK_DEG_S = 400.0
MIN_YAW_TRAVEL = 75.0
# AWP: scoped throws look huge at smaller raw yaw; keep convert window the same.
AWP_MIN_PEAK_DEG_S = 240.0
AWP_MIN_YAW_TRAVEL = 50.0
# Kill may land after the snap (body shot, transfer). 0.2s at 64 tick.
# After the snap, heading must stay on the target — a wide swing then a
# separate 10° poke is not a flick (m0NESY 123° AWP review clip).
CONVERT_TICKS = 13
CONVERT_HOLD_DEG = 8.0


def _unwrap_deg(values: list[float]) -> list[float]:
    if not values:
        return []
    out = [float(values[0])]
    for v in values[1:]:
        d = float(v) - out[-1]
        while d > 180.0:
            d -= 360.0
        while d < -180.0:
            d += 360.0
        out.append(out[-1] + d)
    return out


def _heading_delta_deg(a: float, b: float) -> float:
    d = float(b) - float(a)
    while d > 180.0:
        d -= 360.0
    while d < -180.0:
        d += 360.0
    return abs(d)


def _gates(*, awp: bool) -> tuple[float, float]:
    if awp:
        return AWP_MIN_PEAK_DEG_S, AWP_MIN_YAW_TRAVEL
    return MIN_PEAK_DEG_S, MIN_YAW_TRAVEL


def flick_metrics(yaw: list[float], pitch: list[float], *, awp: bool = False) -> dict | None:
    """Aim-window stats. None if the series is too short."""
    if len(yaw) < 10 or len(pitch) < 10 or len(yaw) != len(pitch):
        return None
    yu = _unwrap_deg([float(v) for v in yaw])
    pu = [float(v) for v in pitch]
    d_yaw = [yu[i] - yu[i - 1] for i in range(1, len(yu))]
    d_pitch = [pu[i] - pu[i - 1] for i in range(1, len(pu))]
    speed = [math.hypot(dy, dp) for dy, dp in zip(d_yaw, d_pitch)]
    peak_deg_s = max(speed) * TICKRATE
    n8 = min(SNAP_DIFFS, len(d_yaw))
    yaw_travel = abs(sum(d_yaw[-n8:]))
    min_peak, min_travel = _gates(awp=awp)
    return {
        "peak_deg_s": peak_deg_s,
        "yaw_travel": yaw_travel,
        "is_flick": peak_deg_s >= min_peak and yaw_travel >= min_travel,
    }


def is_flick(yaw: list[float], pitch: list[float], *, awp: bool = False) -> bool:
    """True when the window (last sample = kill tick) is a flick."""
    m = flick_metrics(yaw, pitch, awp=awp)
    return bool(m and m["is_flick"])


def is_flick_converted(
    yaw: list[float],
    pitch: list[float],
    convert_ticks: int = CONVERT_TICKS,
    *,
    awp: bool = False,
) -> bool:
    """True if a yaw snap ends at or up to ``convert_ticks`` before the last sample.

    The snap has to land on the aim that kills: leftover yaw to the kill tick
    must stay within CONVERT_HOLD_DEG (hold / tiny settle, not a new throw).
    """
    if convert_ticks < 0 or len(yaw) < 10 or len(yaw) != len(pitch):
        return False
    last = len(yaw) - 1
    earliest = max(9, last - convert_ticks)
    kill_yaw = yaw[last]
    for end in range(last, earliest - 1, -1):
        if not is_flick(yaw[: end + 1], pitch[: end + 1], awp=awp):
            continue
        if _heading_delta_deg(yaw[end], kill_yaw) <= CONVERT_HOLD_DEG:
            return True
    return False
