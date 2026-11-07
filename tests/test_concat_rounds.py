"""Tests for cs2archive/pov/concat_rounds.py — single-pass concat-demuxer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


import cs2archive.pov.concat_rounds as concat_rounds

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE_LINK = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"


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


def test_concat_voided_round_allows_gap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round(folder, "round-001-tick-1-to-2.mp4")
        _make_fake_round(folder, "round-003-tick-3-to-4.mp4")
        (folder / ".voided_rounds.json").write_text("[2]", encoding="utf-8")
        concat_rounds.concat_rounds(folder)
        assert (folder / "combined.mp4").exists()


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


def _make_fake_round_dur(folder: Path, name: str, duration: float) -> Path:
    vid = folder / name
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={duration}",
         "-c:v", "libx264", "-r", "30", "-pix_fmt", "yuv420p", str(vid)],
        capture_output=True, timeout=30,
    )
    return vid


def test_concat_sidecar_duration_matches_video() -> None:
    """Regression: each batch duration must be probed BEFORE consume.

    Probing `combined` after append recorded cumulative duration as the
    later batch's duration, so total_duration_seconds ≈ 2× video length and
    late round_offsets landed past EOF (Twistzz Cache: 4195s sidecar vs
    2202s video, round 22 at 3093s).
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _make_fake_round_dur(folder, "batch-001-020.mp4", 2.0)
        _make_fake_round_dur(folder, "batch-021-022.mp4", 0.5)
        concat_rounds.concat_rounds(folder)
        combined = folder / "combined.mp4"
        video_dur = _get_duration(combined)
        side = json.loads((folder / "combined.round_offsets.json").read_text())
        assert abs(side["total_duration_seconds"] - video_dur) < 0.15, (
            f"sidecar total {side['total_duration_seconds']:.3f}s != "
            f"video {video_dur:.3f}s (batches={side['batches']})"
        )
        assert abs(side["batches"][0]["duration_seconds"] - 2.0) < 0.15
        assert abs(side["batches"][1]["duration_seconds"] - 0.5) < 0.15
        # Last round offset must be inside the video, not past EOF.
        last_off = side["round_offsets"]["22"]
        assert last_off < video_dur, (
            f"round 22 offset {last_off:.3f}s past video end {video_dur:.3f}s"
        )
        assert last_off > video_dur * 0.5  # roughly in the second batch


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


def _make_click_round(folder: Path, name: str, duration: float = 2.0,
                      fps: int = 60) -> Path:
    """Round clip whose picture flashes at 0.5 s and whose audio clicks there too.

    Picture and sound mark the same instant, so any shift the concat introduces
    shows up as click-time minus flash-time.
    """
    vid = folder / name
    flash_frame = int(0.5 * fps)
    subprocess.run(
        [FFMPEG, "-y",
         "-f", "lavfi", "-i", f"color=c=black:s=192x108:r={fps}:d={duration}",
         "-f", "lavfi", "-i",
         f"aevalsrc=0.9*gt(t\\,0.5)*lt(t\\,0.51):s=48000:d={duration}",
         "-vf", ("drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
                 f"enable='between(n,{flash_frame},{flash_frame})'"),
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-video_track_timescale", "15360", str(vid)],
        capture_output=True, timeout=60, check=True,
    )
    return vid


def _flash_and_click_times(vid: Path, fps: int = 60) -> tuple[list[float], list[float]]:
    r = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(vid), "-vf", "scale=32:18",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=120, check=True)
    fs = 32 * 18
    n = len(r.stdout) // fs
    means = [sum(r.stdout[i * fs:(i + 1) * fs]) / fs for i in range(n)]
    flashes = [i / fps for i in range(1, n) if means[i] > 100 >= means[i - 1]]

    r = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(vid), "-vn", "-ac", "1",
         "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True, timeout=120, check=True)
    import array
    samples = array.array("f")
    samples.frombytes(r.stdout)
    clicks: list[float] = []
    loud_before = False
    for i, s in enumerate(samples):
        loud = abs(s) > 0.3
        if loud and not loud_before:
            clicks.append(i / 48000)
        loud_before = loud
    return flashes, clicks


def test_concat_audio_does_not_drift_across_joins() -> None:
    """REG-01: every round join used to push the audio one AAC frame later.

    Stream-copying the round clips' AAC packets re-inserts each clip's encoder
    delay (~21 ms) plus its audio tail overhang at every join, so the sound
    slid later by ~26 ms per round: the "audio drifts as the video goes on"
    report (YEKINDAR Dust2, 2026-11-01: 0.44 s late over 18 rounds). The video
    must stay a stream copy; the audio is rebuilt on the picture timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        durations = [2.0, 1.5, 2.5, 2.0]
        for i, dur in enumerate(durations, start=1):
            _make_click_round(
                folder, f"round-{i:03d}-tick-{i * 100}-to-{i * 100 + 60}.mp4", dur)
        concat_rounds.concat_rounds(folder)
        combined = folder / "combined.mp4"

        flashes, clicks = _flash_and_click_times(combined)
        assert len(flashes) == len(durations), (flashes, clicks)
        assert len(clicks) == len(durations), (flashes, clicks)
        offsets = [(c - f) * 1000 for f, c in zip(flashes, clicks)]
        # No accumulation: the last join may not be measurably later than the
        # first one. The old copy join grew by ~29 ms per join (~90 ms here).
        assert abs(offsets[-1] - offsets[0]) < 15, offsets
        assert max(abs(o) for o in offsets) < 40, offsets

        # And the audio track must not outrun the picture.
        r = subprocess.run(
            [FFPROBE_LINK, "-v", "error", "-show_entries",
             "stream=codec_type,duration", "-of", "json", str(combined)],
            capture_output=True, text=True, timeout=30, check=True)
        streams = json.loads(r.stdout)["streams"]
        v = next(s for s in streams if s["codec_type"] == "video")
        a = next(s for s in streams if s["codec_type"] == "audio")
        excess = float(a["duration"]) - float(v["duration"])
        assert excess < 0.05, f"audio outruns picture by {excess*1000:.0f} ms"


if __name__ == "__main__":
    test_concat_one_batch()
    test_concat_three_batches()
    test_concat_empty_folder()
    test_concat_gap_detected()
    test_concat_overlap_detected()
    test_concat_multi_round_batches()
    test_concat_sidecar_duration_matches_video()
    test_concat_resume_partial()
    test_concat_audio_does_not_drift_across_joins()
    print("PASS")
