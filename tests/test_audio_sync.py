"""Delivered-audio sync: what was measured, and what must not regress.

Measured on a real 120 s window of a flameZ POV (``combined.mp4`` = the CSDM
capture, used as the sync reference):

* ``-af volume=0.85 -c:a aac``  -> **+0.0 ms**  (the AAC priming is gapless-tagged
  by this muxer, so the volume filter costs nothing — commit 20f99bf is innocent)
* same + ``atrim=start_sample=1024`` -> **+23.2 ms LATE** (so never trim it here)
* ``faceit/intro_prepend`` on the delivered render -> **-20.6 ms EARLY** because
  ``acrossfade=d=0.02`` overlaps 20 ms of audio at a joint the video cuts hard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from cs2archive.audio_sync import crossfade_pad_filter, crossfade_pad_ms
from cs2archive.pov.assemble_hook import build_hook_filter

SR = 8000


def _lag(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    corr = np.correlate(a, b, mode="full")
    return (int(np.argmax(np.abs(corr))) - (len(b) - 1)) / SR


def _pcm(path: Path, start: float, dur: float) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float64)


def test_crossfade_pad_math() -> None:
    assert crossfade_pad_ms(0.02) == 20
    assert crossfade_pad_filter(0.02) == "adelay=20:all=1"


def test_remux_keeps_audio_sample_locked(monkeypatch, tmp_path) -> None:
    """The overlay remux must NOT carry a priming trim (measured +23 ms late)."""
    from cs2archive.overlay import overlay_encode

    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (tmp_path / "v.mp4.resync.mp4").write_bytes(b"x")

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    monkeypatch.setattr(overlay_encode.subprocess, "run", fake_run)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    overlay_encode._remux_source_audio(video, video)

    af = seen["cmd"][seen["cmd"].index("-af") + 1]
    assert af == "volume=0.85"
    assert "atrim" not in af


def test_hook_join_does_not_trim_priming() -> None:
    """`concat` does not overlap, so the hook join needs no compensation."""
    graph = build_hook_filter([0, 1], [2, 3], end_fade=0.5, total=20.0,
                              width=2560, height=1440, fps=60)

    assert "atrim" not in graph
    assert "volume=0.85" in graph


@pytest.mark.skipif(subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0,
                    reason="ffmpeg required")
def test_prepend_keeps_the_pov_audio_aligned(tmp_path: Path) -> None:
    """Real integration check of the prepend join (video hard cut + acrossfade)."""
    from cs2archive.faceit.intro_prepend import prepend

    hook = tmp_path / "hook.mp4"
    pov = tmp_path / "pov.mp4"
    for path, seconds, seed in ((hook, 2.0, 11), (pov, 4.0, 29)):
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=black:s=320x240:r=30:d={seconds}",
            "-f", "lavfi", "-i", f"anoisesrc=color=white:seed={seed}:duration={seconds}:amplitude=0.5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", str(path),
        ], check=True)
    out = prepend(hook, pov, tmp_path / "joined.mp4")

    # The POV body starts where the hook's picture ends; audio there must still
    # line up with the POV's own audio (before the pad it ran one fade early).
    lag = _lag(_pcm(pov, 0.5, 2.0), _pcm(out, 2.5, 2.0))
    assert abs(lag) <= 0.03, f"prepend shifted the POV audio by {lag * 1000:+.1f} ms"
