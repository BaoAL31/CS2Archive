"""The batch concat muxes the source audio in the SAME pass (Opt 5).

Batch encodes are video-only, so the concatenated overlay has no audio. The
source track used to be attached by a second full-file pass
(``_remux_source_audio``). It is now muxed during the concat, which must be
audio-identical: same AAC 48k stereo normalization, same ``volume=0.85``, no
priming trim, and the same sample-locked timing.

These tests compare the folded path against the two-pass path on generated
media with tone bursts at known offsets. They are SYNTHETIC: they prove the
fold does not move audio against the picture, not that a real CSDM capture is
in sync.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from cs2archive.config import settings
from cs2archive.overlay.overlay_encode import (
    _concat_overlay_batches,
    _remux_source_audio,
)

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe

W, H, FPS = 640, 360, 30
ONSETS = (0.5, 2.5)


def _make_source(path: Path, seconds: float = 4.0) -> Path:
    """A/V source whose audible landmarks sit at ONSETS."""
    pulses = "+".join(f"between(t,{t},{t + 0.3})" for t in ONSETS)
    subprocess.run(
        [FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:d={seconds}",
         "-f", "lavfi", "-i",
         f"aevalsrc='0.6*sin(2*PI*1000*t)*({pulses})':s=48000:d={seconds}:c=stereo",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "30",
         "-c:a", "aac", "-ar", "48000", "-ac", "2", str(path)],
        capture_output=True, check=True, timeout=120,
    )
    return path


def _make_batch(path: Path, seconds: float) -> Path:
    """A video-only batch, exactly what _ffmpeg_encode(include_audio=False) writes."""
    subprocess.run(
        [FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:d={seconds}",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "30",
         "-an", str(path)],
        capture_output=True, check=True, timeout=120,
    )
    assert path.stat().st_size > 100_000
    return path


def _duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    return float(r.stdout.strip())


def _audio_params(path: Path) -> tuple[str, int, int] | None:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    out = r.stdout.strip()
    if not out:
        return None
    codec, rate, ch = out.split(",")[:3]
    return codec, int(rate), int(ch)


def _onsets(path: Path, noise_db: int = -40) -> list[float]:
    r = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    total = _duration(path)
    return [t for t in (float(m) for m in re.findall(r"silence_end: ([0-9.]+)", r.stderr))
            if t < total - 0.1]


@pytest.fixture
def parts(tmp_path):
    source = _make_source(tmp_path / "source.mp4")
    b1 = _make_batch(tmp_path / "batch-001-001.mp4", 2.0)
    b2 = _make_batch(tmp_path / "batch-002-002.mp4", 2.0)
    return source, [b1, b2]


def test_folded_concat_produces_house_audio(parts, tmp_path):
    source, batches = parts
    out = tmp_path / "folded.mp4"
    _concat_overlay_batches(batches, out, source_audio=source)
    assert _audio_params(out) == ("aac", 48000, 2)
    assert abs(_duration(out) - 4.0) < 0.2


def test_folded_concat_keeps_audio_on_the_picture(parts, tmp_path):
    source, batches = parts
    out = tmp_path / "folded.mp4"
    _concat_overlay_batches(batches, out, source_audio=source)
    onsets = _onsets(out)
    assert len(onsets) == len(ONSETS), onsets
    for got, want in zip(onsets, ONSETS):
        assert abs(got - want) < 0.06, onsets


def test_folded_concat_matches_the_two_pass_remux(parts, tmp_path):
    """The fold must be audio-identical to concat-then-remux."""
    source, batches = parts
    folded = tmp_path / "folded.mp4"
    _concat_overlay_batches(batches, folded, source_audio=source)

    two_pass = tmp_path / "two_pass.mp4"
    _concat_overlay_batches(batches, two_pass)
    assert _audio_params(two_pass) is None, "batches must stay video-only"
    _remux_source_audio(two_pass, source)

    folded_onsets = _onsets(folded)
    two_pass_onsets = _onsets(two_pass)
    assert len(folded_onsets) == len(two_pass_onsets) == len(ONSETS)
    for a, b in zip(folded_onsets, two_pass_onsets):
        assert abs(a - b) < 0.02, (folded_onsets, two_pass_onsets)
    assert abs(_duration(folded) - _duration(two_pass)) < 0.1


def test_concat_without_source_audio_stays_video_only(parts, tmp_path):
    _source, batches = parts
    out = tmp_path / "video_only.mp4"
    _concat_overlay_batches(batches, out)
    assert _audio_params(out) is None


def test_folded_concat_rejects_a_source_with_no_audio(tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate={FPS}:d=2",
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(silent)],
        capture_output=True, check=True, timeout=120,
    )
    b1 = _make_batch(tmp_path / "batch-001-001.mp4", 2.0)
    with pytest.raises(SystemExit):
        _concat_overlay_batches([b1], tmp_path / "out.mp4", source_audio=silent)
