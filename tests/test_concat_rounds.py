"""Tests for scripts/concat_rounds.py — batch-based incremental concat."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import concat_rounds

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"


def _make_fake_round(folder: Path, name: str) -> Path:
    vid = folder / name
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.5",
         "-c:v", "libx264", "-r", "30", "-pix_fmt", "yuv420p", str(vid)],
        capture_output=True, timeout=30,
    )
    return vid


def test_concat_one_batch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-001.mp4")
        result = concat_rounds.concat_rounds(folder)
        combined = folder / "combined.mp4"
        assert result == combined
        assert combined.exists()
        assert combined.stat().st_size > 1000
        assert not (folder / "batch-001-001.mp4").exists()


def _get_duration(vid: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(vid)],
        capture_output=True, text=True, timeout=15,
    )
    data = json.loads(r.stdout)
    return float(data["format"]["duration"])


def test_concat_three_batches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-001.mp4")
        _make_fake_round(folder, "batch-002-002.mp4")
        _make_fake_round(folder, "batch-003-003.mp4")
        concat_rounds.concat_rounds(folder)
        combined = folder / "combined.mp4"
        assert combined.exists()
        assert not (folder / "batch-001-001.mp4").exists()
        assert not (folder / "batch-002-002.mp4").exists()
        assert not (folder / "batch-003-003.mp4").exists()
        dur = _get_duration(combined)
        assert 1.2 < dur < 2.0, f"expected ~1.5s, got {dur:.2f}s"


def test_concat_empty_folder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        try:
            concat_rounds.concat_rounds(folder)
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass


def test_concat_gap_detected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-002.mp4")
        _make_fake_round(folder, "batch-004-005.mp4")
        try:
            concat_rounds.concat_rounds(folder)
            assert False, "expected ValueError for gap"
        except ValueError as e:
            assert "gap" in str(e).lower()


def test_concat_overlap_detected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-003.mp4")
        _make_fake_round(folder, "batch-003-005.mp4")
        try:
            concat_rounds.concat_rounds(folder)
            assert False, "expected ValueError for overlap"
        except ValueError as e:
            assert "overlap" in str(e).lower()


def test_concat_multi_round_batches() -> None:
    """Test with batches covering multiple rounds each."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-003.mp4")
        _make_fake_round(folder, "batch-004-006.mp4")
        concat_rounds.concat_rounds(folder)
        combined = folder / "combined.mp4"
        assert combined.exists()
        dur = _get_duration(combined)
        assert 0.8 < dur < 1.5, f"expected ~1.0s, got {dur:.2f}s"


def test_concat_resume_partial() -> None:
    """Simulate crash after 2/3 batches: combined.mp4 already has
    batches 1-2, batch-003-003.mp4 still on disk. On resume only
    batch-003-003 is appended."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "batch-001-001.mp4")
        _make_fake_round(folder, "batch-002-002.mp4")
        concat_rounds.concat_rounds(folder)
        dur_before = _get_duration(folder / "combined.mp4")
        assert 0.8 < dur_before < 1.5, f"expected ~1.0s, got {dur_before:.2f}s"
        _make_fake_round(folder, "batch-003-003.mp4")
        concat_rounds.concat_rounds(folder)
        dur = _get_duration(folder / "combined.mp4")
        assert 1.2 < dur < 2.5, f"expected ~1.5s, got {dur:.2f}s"
        assert dur > dur_before + 0.2, f"expected growth, got {dur_before:.2f}s -> {dur:.2f}s"
        assert not (folder / "batch-003-003.mp4").exists()


if __name__ == "__main__":
    test_concat_one_batch()
    test_concat_three_batches()
    test_concat_empty_folder()
    test_concat_gap_detected()
    test_concat_overlap_detected()
    test_concat_multi_round_batches()
    test_concat_resume_partial()
    print("PASS")
