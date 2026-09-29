"""Content alignment for the intro -> round-1 joint (intro_prepend).

Two independent CSDM/HLAE launches never land frame-exactly on the same tick,
so tick-math abutment leaves a camera snap whenever the player moves near the
joint. The fix renders overlap into round 1 and cuts where the main video's
head matches inside the footage (find_head_match).
"""
from __future__ import annotations

import numpy as np

from cs2archive.faceit.intro_prepend import find_head_match


def _moving_bar(n, w=320, h=36):
    """n grayscale frames with a bright bar sliding right (unique positions)."""
    frames = []
    for i in range(n):
        img = np.zeros((h, w), dtype=np.uint8)
        x = (i * 7) % (w - 8)
        img[:, x:x + 8] = 200
        # Second marker breaks the stride period so offsets are unambiguous.
        y = (i * 13) % h
        img[y, :] = 120
        frames.append(img)
    return frames


def _static(n, w=64, h=36, level=100):
    rng = np.random.default_rng(7)
    base = (np.ones((h, w), dtype=np.uint8) * level)
    return [base + rng.integers(0, 3, size=(h, w)).astype(np.uint8)
            for _ in range(n)]


def _noisy_copy(frames, seed=3, sigma=4.0):
    """Simulate a second encode generation: same content + light noise."""
    rng = np.random.default_rng(seed)
    out = []
    for f in frames:
        n = f.astype(np.float32) + rng.normal(0, sigma, f.shape)
        out.append(np.clip(n, 0, 255).astype(np.uint8))
    return out


def test_gap_case_finds_true_offset():
    # Footage tail covers ticks [0, 200); main head starts at tick 150
    # (a 50-tick gap past the tick-math cut at 100).
    seg = _moving_bar(200)
    main = _noisy_copy(seg[150:150 + 60])
    off, method = find_head_match(seg, main, expected=100, window=120)
    assert method == "aligned"
    assert off == 150


def test_overlap_case_finds_true_offset():
    # Main head starts BEFORE the tick-math cut (round clip began early).
    seg = _moving_bar(200)
    main = _noisy_copy(seg[60:60 + 60])
    off, method = find_head_match(seg, main, expected=100, window=120)
    assert method == "aligned"
    assert off == 60


def test_exact_tick_math_keeps_tick_math():
    seg = _moving_bar(200)
    main = _noisy_copy(seg[100:100 + 60])
    off, method = find_head_match(seg, main, expected=100, window=120)
    assert off == 100
    assert method == "tick-math"


def test_static_footage_keeps_tick_math():
    # Freeze-time static content: every offset matches, so there is nothing
    # to correct (any cut is seamless) — must not drift.
    seg = _static(200)
    main = [f.copy() for f in seg[100:100 + 60]]
    off, method = find_head_match(seg, main, expected=100, window=120)
    assert method == "tick-math"
    assert off == 100


def test_garbage_main_keeps_tick_math():
    # Nothing matches (e.g. overlay graphics over the main head): no
    # trustworthy correction, keep tick-math.
    seg = _moving_bar(200)
    rng = np.random.default_rng(11)
    main = [rng.integers(0, 255, size=(36, 320)).astype(np.uint8)
            for _ in range(60)]
    off, method = find_head_match(seg, main, expected=100, window=120)
    assert method == "tick-math"
    assert off == 100


def test_short_inputs_fall_back():
    seg = _moving_bar(10)
    off, method = find_head_match(seg, [], expected=5)
    assert (off, method) == (5, "tick-math")
