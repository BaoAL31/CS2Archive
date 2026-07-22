"""Tests for scripts/pov/generate_outro.py"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

import generate_outro

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"


def test_render_frame_size() -> None:
    img = generate_outro.render_frame(1920, 1080)
    assert img.size == (1920, 1080)


def test_render_frame_size_1440p() -> None:
    img = generate_outro.render_frame(2560, 1440)
    assert img.size == (2560, 1440)


def test_render_frame_has_text() -> None:
    img = generate_outro.render_frame(1920, 1080)
    extrema = img.getextrema()
    assert extrema != ((0, 0), (0, 0), (0, 0)), "expected text rendered on black bg"


def test_render_frame_black_bg() -> None:
    img = generate_outro.render_frame(1920, 1080)
    max_vals = [c[1] for c in img.getextrema()]
    assert all(v == 255 for v in max_vals), "text should use white/bright pixels"


def test_get_video_info() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        vid = Path(tmp) / "test.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
             "-c:v", "libx264", "-r", "30", "-pix_fmt", "yuv420p", str(vid)],
            capture_output=True, timeout=30,
        )
        w, h, fps = generate_outro.get_video_info(vid)
        assert w == 320
        assert h == 240
        assert abs(fps - 30.0) < 0.1


if __name__ == "__main__":
    test_render_frame_size()
    test_render_frame_size_1440p()
    test_render_frame_has_text()
    test_render_frame_black_bg()
    test_get_video_info()
    print("PASS")
