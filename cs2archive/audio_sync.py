"""Audio-sync diagnostics and historical capture measurements.

WARNING: kill-tick/first-amplitude-crossing fits are not validated picture/shot
landmarks. Automatic overlay corrections are disabled: near-unity atempo added
+314 ms over 1658 s on retained ZywOo audio, while plain AAC encoding added none.
The signed fits below must not drive production correction without independent
visible-shot validation. See docs/bugs/audio-sync-atempo-drift.md.

Historical observations on renders (flameZ / Dust2, 25 min POV):

1. **The volume change is innocent.** ``-af volume=0.85 -c:a aac`` keeps the
   delivered audio sample-locked to the CSDM capture (**+0.0 ms**, 120 s window
   vs ``combined.mp4``). Adding a 1024-sample AAC priming trim makes it
   **+23.2 ms late**, so the priming must NOT be trimmed — this muxer tags it
   (gapless). The voice mix trims it (cf263a2) because that path sums raw PCM
   without gapless tagging: different path, different answer.

2. **``acrossfade`` shortens the joined audio.** ``faceit/intro_prepend`` joins
   the hook/intro audio to the POV audio with ``acrossfade=d=<fade>``, which
   removes one fade length of audio at a joint the video cuts hard — a constant
   **-20.6 ms** on the POV body. Pad it back (``crossfade_pad_filter``).

3. **The drift is a rate mismatch in the raw capture.** Audio-vs-video offset
   measured with demo kill landmarks (audio peak vs the tick the frame should
   carry), two kills 1088 s apart: ``+0.327 s @176 s`` → ``+0.105 s @1264 s``,
   i.e. **-0.20 ms/s** (~200 ppm). Cross-correlating 20 s windows down the file
   gives the same -0.25 ms/s. It is already present in ``combined.mp4`` —
   before the overlay, the volume filter, or the hook — so no muxing change can
   cause or cure it; the delivered audio has to be resampled onto the video
   timeline (``atempo``).

Short renders never showed it (200 ppm only becomes audible over 10+ minutes),
which is why the muxing-drift fixes (1606d6e, cf263a2, c7e29a9, 2e5b6ab) never
closed it.

4. **The drift *fit* was the remaining defect (2026-10-11).** The slope was fit
   from **6 latched marks** with plain least squares, and logs across nine
   renders show what that produced::

       +0.637 +0.294 +0.200 +0.076 -0.058 -0.253 -0.283 -0.474 -0.574  ms/s

   random signs, against a real drift that is consistently *positive* (audio
   progressively late). The onset finder also latched onto the window's edge:
   when a louder sound sat earlier than the expected picture time the reported
   offset came out as ``-(window - ~0.1)`` — the repeated ``-0.59 s`` cluster on
   the donk Dust2 POV. A wrong-signed fit does not merely fail to help; slowing
   the audio adds delay toward the end of the file (``-0.574 ms/s`` on 21 min =
   ~+0.8 s). Fix: many marks (24+), edge-latch rejection, and a Theil-Sen
   (median-of-pairwise-slopes) fit, which an outlier cannot move.
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

AAC_PRIMING_SAMPLES = 1024  # informational: tagged by this muxer, do not trim
PROBE_RATE = 48000
MIN_MARKS = 2
# Fewer marks than this and no slope is trustworthy: with only a couple of
# landmarks a single latched onset sets the correction (the 6-mark era).
MIN_SLOPE_MARKS = 8
# A drift beyond this is a measurement error, not a render: never "fix" it.
# 0.5 % is 5 ms/s — 4.5x the largest drift ever measured (1.1 ms/s, donk Dust2
# 2026-10-11), and the old 2 % ceiling happily accepted garbage fits.
MAX_TEMPO_DEVIATION = 0.005
# Onsets this close to the window's leading edge mean the loudest sound sat
# BEFORE the expected picture time (a previous engagement), not at it.
EDGE_REJECT_SECONDS = 0.12


def crossfade_pad_ms(fade_seconds: float) -> int:
    return int(round(float(fade_seconds) * 1000))


def crossfade_pad_filter(fade_seconds: float) -> str:
    """Delay that restores the timeline an ``acrossfade`` of this length removed."""
    return f"adelay={crossfade_pad_ms(fade_seconds)}:all=1"


def fit_drift(marks: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares ``(intercept, slope)`` of (video_time, audio_offset) samples.

    ``slope`` is the drift in seconds of audio offset per second of video; the
    intercept is deliberately not used for compensation (the absolute offset of a
    gunshot peak depends on which transient the peak finder latches onto, while
    the slope compares like with like).
    """
    samples = [(float(t), float(o)) for t, o in marks]
    if len(samples) < MIN_MARKS:
        return 0.0, 0.0
    n = len(samples)
    mean_t = sum(t for t, _ in samples) / n
    mean_o = sum(o for _, o in samples) / n
    denom = sum((t - mean_t) ** 2 for t, _ in samples)
    if denom <= 0:
        return 0.0, 0.0
    slope = sum((t - mean_t) * (o - mean_o) for t, o in samples) / denom
    return mean_o - slope * mean_t, slope


def theil_sen_slope(marks: list[tuple[float, float]]) -> float:
    """Median of pairwise slopes — the drift, unmovable by one bad mark.

    Least squares gave a random-signed slope from 6 marks (see module
    docstring): a single onset that latched onto the wrong sound could swing the
    correction from "speed the audio up" to "slow it down", and a wrong-signed
    correction makes the delivered delay grow instead of shrink.
    """
    samples = sorted((float(t), float(o)) for t, o in marks)
    if len(samples) < MIN_SLOPE_MARKS:
        return 0.0
    slopes: list[float] = []
    for i, (t_i, o_i) in enumerate(samples):
        for t_j, o_j in samples[i + 1:]:
            if t_j > t_i:
                slopes.append((o_j - o_i) / (t_j - t_i))
    if not slopes:
        return 0.0
    slopes.sort()
    mid = len(slopes) // 2
    return slopes[mid] if len(slopes) % 2 else 0.5 * (slopes[mid - 1] + slopes[mid])


def robust_intercept(marks: list[tuple[float, float]], slope: float) -> float:
    """Median residual offset — the constant part, outlier-proof."""
    residuals = sorted(float(o) - slope * float(t) for t, o in marks)
    if not residuals:
        return 0.0
    mid = len(residuals) // 2
    return residuals[mid] if len(residuals) % 2 else 0.5 * (residuals[mid - 1] + residuals[mid])


def atempo_for_slope(slope: float) -> float:
    """Tempo factor that cancels a drift slope (1.0 = no correction).

    A negative slope means the audio creeps ahead of the picture, so the audio
    has to be slowed down: ``1 + slope`` < 1.
    """
    factor = 1.0 + float(slope)
    if abs(factor - 1.0) > MAX_TEMPO_DEVIATION:
        return 1.0
    return factor


def _audio_samples(video_path: Path, start: float, duration: float) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(video_path), "-vn", "-ac", "1", "-ar", str(PROBE_RATE),
         "-f", "f32le", "-"],
        capture_output=True, check=True).stdout
    count = len(out) // 4
    return list(struct.unpack(f"<{count}f", out[: count * 4]))


def _audio_onset_offset(video_path: Path, expected: float, window: float,
                        *, threshold: float = 0.4) -> float | None:
    """Offset of the gunshot ONSET near ``expected`` (None when the seek failed).

    The onset (first sample crossing ``threshold`` of the window's peak) is a far
    sharper landmark than the peak itself: a shot's peak is a decaying tail that
    the window may latch onto late, which is the ±100-300 ms noise that made the
    first fit of this drift untrustworthy.
    """
    start = max(0.0, expected - window)
    try:
        samples = _audio_samples(video_path, start, window * 2)
    except (subprocess.CalledProcessError, OSError):
        return None
    if not samples:
        return None
    peak = max(abs(s) for s in samples)
    if peak <= 0:
        return None
    limit = peak * threshold
    for index, value in enumerate(samples):
        if abs(value) >= limit:
            offset = (start + index / float(PROBE_RATE)) - expected
            # Onset hugging the leading edge = the loudest sound was BEFORE the
            # expected picture time (a previous engagement, a teammate's shot).
            # Those marks read as a constant -0.59 s on a 0.7 s window and were
            # what poisoned the old fit.
            if offset <= -(window - EDGE_REJECT_SECONDS):
                return None
            return offset
    return None


def _pov_kills(demo_path: Path, steam_id: str) -> list[int]:
    try:
        import demoparser2 as dp
    except ImportError:
        return []
    parser = dp.DemoParser(str(demo_path))
    deaths = parser.parse_event("player_death")
    if deaths is None or len(deaths) == 0:
        return []
    attacker = deaths["attacker_steamid"].astype(str)
    victim = deaths["user_steamid"].astype(str)
    # One mask, applied once: filtering twice reindexes the boolean against the
    # already-filtered frame (pandas warning) and can drop the wrong rows.
    core = deaths[(attacker == str(steam_id)) & (attacker != victim)]
    try:
        round_starts = parser.parse_event("round_start")
        r1 = round_starts[round_starts["round"] == 1]
        if len(r1):
            core = core[core["tick"] >= int(r1["tick"].max())]  # knife round out
    except Exception:  # noqa: BLE001 - no round events: keep every kill
        pass
    return sorted({int(t) for t in core["tick"]})


def measure_audio_drift(
    video_path: Path,
    demo_path: Path,
    steam_id: str,
    round_offsets: dict[int, float],
    per_round_ticks: dict[int, tuple[int, int]] | None = None,
    *,
    tickrate: int = 64,
    max_marks: int = 24,
    window: float = 0.7,
) -> tuple[float, float, int]:
    """``(intercept, slope, n_marks)`` for the audio drift of an already-rendered POV.

    Kill ticks are projected through the round sidecar into video time: for each
    kill we know where the *picture* should be, and the loudest sample nearby is
    where the *audio* is. Marks are spread across the file so the slope compares
    the start of the match against its end.

    The slope is Theil-Sen and needs ``MIN_SLOPE_MARKS`` valid marks; a thin or
    edge-latched sample returns slope 0.0, i.e. "no correction", because a
    wrong-signed correction is worse than none (see module docstring).
    """
    if not round_offsets or not per_round_ticks:
        return 0.0, 0.0, 0
    spans = {int(k): (int(v[0]), int(v[1])) for k, v in per_round_ticks.items()}
    marks: list[tuple[float, float]] = []
    for tick in _pov_kills(Path(demo_path), str(steam_id)):
        for rnd, (start_tick, end_tick) in spans.items():
            if not (start_tick <= tick <= end_tick):
                continue
            expected = float(round_offsets.get(rnd, 0.0)) + (tick - start_tick) / float(tickrate)
            marks.append((expected, tick))
            break
    if len(marks) < MIN_MARKS:
        return 0.0, 0.0, 0
    marks.sort()
    step = max(1, len(marks) // max_marks)
    chosen = marks[::step][:max_marks]
    if chosen[-1] != marks[-1]:
        chosen[-1] = marks[-1]

    samples: list[tuple[float, float]] = []
    for expected, _tick in chosen:
        offset = _audio_onset_offset(Path(video_path), expected, window)
        if offset is not None:
            samples.append((expected, offset))
    slope = theil_sen_slope(samples)
    return robust_intercept(samples, slope), slope, len(samples)
