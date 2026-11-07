"""Audio drift measurement: the fit must not be steerable by one bad mark.

The delivered-audio delay went unreported-as-fixed for weeks because the slope
came from 6 latched onsets fitted with least squares, which produced
random-signed corrections (+0.637 … -0.574 ms/s across nine renders) against a
real drift that is consistently positive (audio progressively late). A
wrong-signed correction makes the delay grow, so "no correction when unsure" is
the safer failure mode.
"""
from __future__ import annotations

from pathlib import Path

from cs2archive import audio_sync as a


def test_theil_sen_recovers_slope_with_an_outlier() -> None:
    true_slope = 0.0008  # 0.8 ms/s, the order measured on real POVs
    marks = [(t, 0.05 + true_slope * t) for t in range(0, 400, 50)]
    polluted = [*marks, (200.0, 3.0)]  # one onset that latched onto a louder sound
    slope = a.theil_sen_slope(polluted)
    assert abs(slope - true_slope) < 1e-4, slope
    # Least squares on the same 9 marks is dragged off by the outlier (the
    # 6-mark era's failure mode); Theil-Sen is far closer to the truth.
    _intercept, ls_slope = a.fit_drift(polluted)
    assert abs(ls_slope - true_slope) > 10 * abs(slope - true_slope), (
        slope, ls_slope)


def test_theil_sen_needs_enough_marks() -> None:
    few = [(float(t), 0.001 * t) for t in range(0, 5)]
    assert len(few) < a.MIN_SLOPE_MARKS
    assert a.theil_sen_slope(few) == 0.0


def test_robust_intercept_is_the_median_residual() -> None:
    marks = [(t, 0.2 - 0.5 * t) for t in range(0, 400, 20)]
    marks.append((400.0, 9.9))
    intercept = a.robust_intercept(marks, -0.5)
    assert abs(intercept - 0.2) < 0.05, intercept


def test_tempo_direction_and_cap() -> None:
    # Audio progressively late -> speed it up (tempo > 1) to pull it back.
    assert a.atempo_for_slope(0.0008) > 1.0
    # Audio creeping ahead -> slow it down.
    assert a.atempo_for_slope(-0.0008) < 1.0
    # Beyond the sanity cap: refuse to "fix" it.
    assert a.atempo_for_slope(2.0) == 1.0
    assert a.atempo_for_slope(0.0) == 1.0
    assert a.MAX_TEMPO_DEVIATION <= 0.005


def _fake_marks(monkeypatch, ticks: list[int], onset) -> None:
    monkeypatch.setattr(a, "_pov_kills", lambda demo, steam: ticks)
    monkeypatch.setattr(a, "_audio_onset_offset",
                        lambda video, expected, window: onset(expected))


def test_measure_audio_drift_ignores_a_wild_mark(monkeypatch) -> None:
    rounds = {r: (r * 1000, r * 1000 + 900) for r in range(1, 25)}
    offsets = {r: (r - 1) * 55.0 for r in range(1, 25)}
    ticks = [r * 1000 + 100 for r in range(1, 25)]  # one kill inside each round
    true_slope = 0.0007

    def onset(expected: float) -> float:
        if abs(expected - 660.0) < 1.0:  # one mark latched onto the wrong sound
            return 2.5
        return 0.05 + true_slope * expected

    _fake_marks(monkeypatch, ticks, onset)
    _intercept, slope, n = a.measure_audio_drift(
        Path("v.mp4"), Path("d.dem"), "1", offsets, rounds)
    assert n >= a.MIN_SLOPE_MARKS
    assert abs(slope - true_slope) < 2e-4, slope


def test_measure_audio_drift_without_marks_makes_no_correction(monkeypatch) -> None:
    _fake_marks(monkeypatch, [], lambda expected: None)
    _intercept, slope, n = a.measure_audio_drift(
        Path("v.mp4"), Path("d.dem"), "1", {1: 0.0}, {1: (0, 100)})
    assert slope == 0.0 and n == 0
    assert a.atempo_for_slope(slope) == 1.0
