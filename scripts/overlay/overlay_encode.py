"""ffmpeg encode / segment / concat helpers for the overlay pipeline."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from overlay._common import _log

def _overlay_output_valid(path: Path) -> bool:
    """Return True if a batch/final overlay file is present and non-empty."""
    return path.is_file() and path.stat().st_size > 100_000


def _compute_batch_boundaries(
    round_offsets: dict[int, float],
    fps: float,
    frame_count: int,
    batch_size: int,
) -> list[tuple[int, int, int, float, float]]:
    """Group sorted rounds into chunks of ``batch_size`` and return
    ``[(round_start, round_end, batch_start_frame, batch_start_sec, batch_end_sec), ...]``.

    The last batch's end_sec clamps to ``frame_count / fps``. ``batch_end_sec``
    for intermediate batches is the start_sec of the next batch's first round.
    """
    if batch_size < 1 or not round_offsets:
        return []
    sorted_rounds = sorted(round_offsets.keys())
    total_seconds = frame_count / fps
    boundaries: list[tuple[int, int, int, float, float]] = []
    for i in range(0, len(sorted_rounds), batch_size):
        chunk = sorted_rounds[i:i + batch_size]
        rn_start, rn_end = chunk[0], chunk[-1]
        start_sec = float(round_offsets[rn_start])
        if i + batch_size < len(sorted_rounds):
            end_sec = float(round_offsets[sorted_rounds[i + batch_size]])
        else:
            end_sec = total_seconds
        start_frame = int(start_sec * fps)
        boundaries.append((rn_start, rn_end, start_frame, start_sec, end_sec))
    return boundaries




def _ffmpeg_encode(
    main_input: str,
    extra_inputs: list[Path],
    fc_args: list[str],
    out_label: str,
    output_path: str,
    segment: tuple[float, float] | None = None,
) -> None:
    """Run ffmpeg with h264_nvenc. No CPU fallback (libx forbidden by user).

    When ``segment`` is set, ``-ss {start} -to {end}`` is applied as INPUT
    options on the main video so both video and audio streams are trimmed
    frame-accurately by ffmpeg's demuxer. Keyframe-aligned (input-side
    seeking is fast; visible round-boundary jumps are avoided by the
    round_offsets sidecar using actual per-round frames).

    Atomic write: ffmpeg renders to ``{output}.part`` and the file is
    renamed onto ``output_path`` only after a successful exit. A cancelled /
    crashed encode therefore leaves a stale ``.part`` (never the final name),
    so resume checks (``_overlay_output_valid``) cannot mistake a partial
    file for a complete one.
    """
    out_path = Path(output_path)
    tmp_path = out_path.with_name(out_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y"]
    if segment is not None:
        start_sec, end_sec = segment
        if start_sec > 0:
            cmd.extend(["-ss", f"{start_sec:.6f}"])
        cmd.extend(["-to", f"{end_sec:.6f}"])
    cmd.extend(["-i", main_input])
    for inp in extra_inputs:
        cmd.extend(["-i", str(inp)])
    cmd.extend([
        *fc_args, "-map", out_label, "-map", "0:a?", "-shortest",
        # Match raw concat quality (concat_rounds.py): cq 16 / p7
        "-c:v", "h264_nvenc", "-cq", "16", "-preset", "p7",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "256k",
        "-af", "asetpts=PTS-STARTPTS",
        "-movflags", "+faststart",
        "-g", "60", "-keyint_min", "60",
        "-f", "mp4", str(tmp_path),
    ])
    _log(f"  [ffmpeg] nvenc preset p7 cq 16 (match raw; no libx fallback)")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=21600)  # 6h
    if result.returncode != 0 or not tmp_path.is_file():
        _log(f"[ERROR] nvenc ffmpeg failed: rc={result.returncode}")
        _log(f"  stderr: {(result.stderr or '')[-400:]}")
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)
    os.replace(tmp_path, out_path)




def _ffmpeg_segment_copy(
    video_path: Path,
    start_sec: float,
    end_sec: float,
    output_path: Path,
) -> None:
    """Stream-copy a video segment when no overlay applies to this batch.

    Fast path (no encode) used when a batch has zero key presses AND zero
    flight PiP clips — output is byte-identical (codec params) to the
    other batch-overlay-*.mp4 files so the final concat stream-copy works.
    """
    tmp_path = output_path.with_name(output_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    cmd = ["ffmpeg", "-y"]
    if start_sec > 0:
        cmd.extend(["-ss", f"{start_sec:.6f}"])
    cmd.extend(["-to", f"{end_sec:.6f}", "-i", str(video_path), "-c", "copy",
                "-movflags", "+faststart", "-f", "mp4", str(tmp_path)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0 or not tmp_path.is_file():
        _log(f"[ERROR] ffmpeg segment copy failed: rc={result.returncode}")
        _log(f"  stderr: {(result.stderr or '')[-400:]}")
        tmp_path.unlink(missing_ok=True)
        sys.exit(1)
    os.replace(tmp_path, output_path)




def _concat_overlay_batches(batch_files: list[Path], output_path: Path) -> None:
    """Concat batch-overlay-*.mp4 files via ffmpeg stream copy (no re-encode).

    Validates the merged file is non-empty. Raises ``SystemExit`` on ffmpeg
    failure. Stream copy requires all inputs to share codec params (same
    _ffmpeg_encode call produces all batches, so this holds).
    """
    if not batch_files:
        _log("[ERROR] no batch files to concat")
        sys.exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "files.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for bf in batch_files:
                f.write(f"file '{bf.resolve()}'\n")
        tmp_path = output_path.with_name(output_path.name + ".part")
        tmp_path.unlink(missing_ok=True)
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not tmp_path.is_file():
            _log(f"[ERROR] ffmpeg batch concat failed: rc={result.returncode}")
            _log(f"  stderr: {(result.stderr or '')[-400:]}")
            tmp_path.unlink(missing_ok=True)
            sys.exit(1)
        os.replace(tmp_path, output_path)
    if not _overlay_output_valid(output_path):
        _log(f"[ERROR] concat output too small: {output_path}")
        sys.exit(1)


# -- CLI -----------------------------------------------------------------


