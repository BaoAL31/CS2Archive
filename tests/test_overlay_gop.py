"""GOP audit verdicts (pure function, no ffmpeg needed)."""
from __future__ import annotations

from cs2archive.overlay.overlay_encode import _check_gop_times


def test_clean_gop_passes():
    assert _check_gop_times([0.0, 1.0, 2.0, 3.0], 6.0) is None
    assert _check_gop_times([0.0, 4.1667, 8.3334], 6.0) is None


def test_gap_violation_fails():
    msg = _check_gop_times([0.0, 1.0, 16.0], 6.0)
    assert msg is not None and "15.0s" in msg


def test_pts_regression_fails():
    # Double-IDR seam wrinkle: 439.39 before 439.34.
    msg = _check_gop_times([438.27, 439.39, 439.34, 442.59], 6.0)
    assert msg is not None and "regression" in msg.lower()


def test_submillisecond_jitter_passes():
    assert _check_gop_times([0.0, 1.0000005, 2.0], 6.0) is None


def test_empty_series_fails():
    assert _check_gop_times([], 6.0) is not None
