"""Overlay batch audio path: batches encode video-only, source audio muxed once.

Regression tests for the single-pass audio change:
  - ``_ffmpeg_encode(..., include_audio=False)`` writes no audio track.
  - ``_remux_source_audio`` restores the source audio via ``-c:a copy``
    (no AAC re-encode) and keeps A/V durations in sync.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

from overlay.overlay_encode import (
    _concat_overlay_batches,
    _ffmpeg_encode,
    _remux_source_audio,
)

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = "ffprobe"


def _make_av(path: Path, duration: float = 2.0) -> None:
    r = subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", f"testsrc=s=320x240:d={duration}:r=30",
         "-f", "lavfi", "-i", f"sine=frequency=440:d={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-shortest", str(path)],
        capture_output=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr[-500:]


def _streams(path: Path) -> list[dict]:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    return json.loads(r.stdout).get("streams", [])


def _duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return float(json.loads(r.stdout)["format"]["duration"])


def test_batch_encode_video_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        src = folder / "src.mp4"
        _make_av(src)
        out = folder / "batch.mp4"
        _ffmpeg_encode(
            str(src), [],
            ["-filter_complex", "[0:v]null[outv]"],
            "[outv]", str(out),
            segment=(0.0, 2.0),
            include_audio=False,
        )
        kinds = {s["codec_type"] for s in _streams(out)}
        assert kinds == {"video"}, f"expected video-only batch, got {kinds}"


def test_remux_restores_copied_audio() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        src = folder / "src.mp4"
        _make_av(src)
        src_audio = next(s for s in _streams(src) if s["codec_type"] == "audio")
        # Two video-only batches -> concat -> remux, mirroring run_overlay.
        b1 = folder / "batch-overlay-001-001.mp4"
        b2 = folder / "batch-overlay-002-002.mp4"
        for b, seg in ((b1, (0.0, 1.0)), (b2, (1.0, 2.0))):
            _ffmpeg_encode(
                str(src), [],
                ["-filter_complex", "[0:v]null[outv]"],
                "[outv]", str(b),
                segment=seg,
                include_audio=False,
            )
        final = folder / "final.overlay.mp4"
        _concat_overlay_batches([b1, b2], final)
        assert {s["codec_type"] for s in _streams(final)} == {"video"}
        _remux_source_audio(final, src)
        streams = _streams(final)
        kinds = {s["codec_type"] for s in streams}
        assert kinds == {"video", "audio"}, f"expected A+V after remux, got {kinds}"
        out_audio = next(s for s in streams if s["codec_type"] == "audio")
        assert out_audio["codec_name"] == src_audio["codec_name"], (
            f"audio re-encoded ({src_audio['codec_name']} -> "
            f"{out_audio['codec_name']}); expected stream copy"
        )
        assert abs(_duration(final) - _duration(src)) < 0.3, (
            f"A/V duration drift: final={_duration(final):.3f}s "
            f"src={_duration(src):.3f}s"
        )


if __name__ == "__main__":
    test_batch_encode_video_only()
    test_remux_restores_copied_audio()
    print("PASS")
