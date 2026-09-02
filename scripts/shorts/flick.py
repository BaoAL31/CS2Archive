"""Flick: a fast snap onto the target in the half-second before the kill.

Not quickscope. Quickscope is out of the kind set.
"""
from __future__ import annotations

import math

TICKRATE = 64.0
PRE_TICKS = 32  # 0.5s lookback
SNAP_DIFFS = 8  # ~125ms at the kill

# Snap vs tracking / already-aimed. Peak ~280 deg/s plus a real yaw throw
# in the last 125ms; tracking sprays stay under this.
MIN_PEAK_DEG_S = 280.0
MIN_YAW_TRAVEL = 40.0
MIN_RATIO = 3.5


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


def flick_metrics(yaw: list[float], pitch: list[float]) -> dict | None:
    """Aim-window stats. None if the series is too short."""
    if len(yaw) < 10 or len(pitch) < 10 or len(yaw) != len(pitch):
        return None
    yu = _unwrap_deg([float(v) for v in yaw])
    pu = [float(v) for v in pitch]
    d_yaw = [yu[i] - yu[i - 1] for i in range(1, len(yu))]
    d_pitch = [pu[i] - pu[i - 1] for i in range(1, len(pu))]
    speed = [math.hypot(dy, dp) for dy, dp in zip(d_yaw, d_pitch)]
    peak_deg_s = max(speed) * TICKRATE
    snap = speed[-SNAP_DIFFS:] if len(speed) >= SNAP_DIFFS else speed
    earlier = speed[:-SNAP_DIFFS] if len(speed) > SNAP_DIFFS else speed[:1]
    n8 = min(SNAP_DIFFS, len(d_yaw))
    yaw_travel = abs(sum(d_yaw[-n8:]))
    earlier_sorted = sorted(earlier)
    p95_i = max(0, math.ceil(0.95 * len(earlier_sorted)) - 1)
    earlier_p95 = earlier_sorted[p95_i]
    ratio = max(snap) / max(earlier_p95, 0.05)
    return {
        "peak_deg_s": peak_deg_s,
        "yaw_travel": yaw_travel,
        "ratio": ratio,
        "is_flick": (
            peak_deg_s >= MIN_PEAK_DEG_S
            and yaw_travel >= MIN_YAW_TRAVEL
            and ratio >= MIN_RATIO
        ),
    }


def is_flick(yaw: list[float], pitch: list[float]) -> bool:
    """True when the window (last sample = kill tick) is a flick."""
    m = flick_metrics(yaw, pitch)
    return bool(m and m["is_flick"])

