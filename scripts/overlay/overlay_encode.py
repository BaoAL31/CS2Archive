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
    loop_inputs: set[str] | None = None,
    raw_inputs: list[tuple[Path, list[str]]] | None = None,
) -> None:
    """Run ffmpeg with h264_nvenc. No CPU fallback (libx forbidden by user).

    When ``segment`` is set, input-side ``-ss`` plus an explicit ``-t``
    duration is applied to the main video. Using ``-to`` here is subtly
    wrong: with input-side seeking ffmpeg can retain the original absolute
    end timestamp, making each independently encoded batch longer than its
    corresponding video interval. Those extra timestamps accumulate when
    batches are concatenated and make the remuxed source audio sound late.
    Keyframe-aligned input seeking is intentional; visible round-boundary
    jumps are avoided by the round_offsets sidecar using actual per-round
    frames.

    ``raw_inputs`` is a list of ``(path, input_options)`` for raw video inputs
    (e.g. 1x1 RGBA alpha controls) that need explicit demuxer flags before
    ``-i`` (``-f rawvideo -pix_fmt rgba -s 1x1 -r <fps>``). They are appended
    AFTER ``extra_inputs`` in input order.

    Atomic write: ffmpeg renders to ``{output}.part`` and the file is
    renamed onto ``output_path`` only after a successful exit. A cancelled /
    crashed encode therefore leaves a stale ``.part`` (never the final name),
    so resume checks (``_overlay_output_valid``) cannot mistake a partial
    file for a complete one.
    """
    out_path = Path(output_path)
    tmp_path = out_path.with_name(out_path.name + ".part")
    tmp_path.unlink(missing_ok=True)

    # Force square pixels on the final label. Without this, an anamorphic
    # master (e.g. 2560x1440 with SAR 3:4 from a 4:3 stretch that omitted
    # setsar=1) makes ffmpeg composite keyboard/PiP sprites with the wrong
    # sample aspect — overlays look permanently squished in the encode.
    map_label = out_label
    fc_out = list(fc_args)
    if fc_out and fc_out[0] == "-filter_complex" and len(fc_out) >= 2:
        fc_out[1] = f"{fc_out[1].rstrip().rstrip(';')};{out_label}setsar=1[__sar1]"
        map_label = "[__sar1]"
    elif fc_out and fc_out[0] == "-filter_complex_script" and len(fc_out) >= 2:
        script = Path(fc_out[1])
        body = script.read_text(encoding="utf-8").rstrip().rstrip(";")
        script.write_text(
            f"{body};{out_label}setsar=1[__sar1]\n", encoding="utf-8"
        )
        map_label = "[__sar1]"

    cmd = ["ffmpeg", "-y"]
    if segment is not None:
        start_sec, end_sec = segment
        if start_sec > 0:
            cmd.extend(["-ss", f"{start_sec:.6f}"])
        duration_sec = max(0.0, end_sec - start_sec)
        cmd.extend(["-t", f"{duration_sec:.6f}"])
    cmd.extend(["-i", main_input])
    loop_set = loop_inputs or set()
    for inp in extra_inputs:
        if str(inp) in loop_set:
            # Loop a still image so it keeps producing frames for the whole
            # encode (framesync filters like alphamerge would otherwise EOF
            # after the first frame). Sprite PNGs fed to `overlay` do NOT
            # need this — overlay's eof_action repeats their last frame.
            cmd.extend(["-loop", "1"])
        cmd.extend(["-i", str(inp)])
    for raw_path, raw_opts in (raw_inputs or []):
        cmd.extend(raw_opts)
        cmd.extend(["-i", str(raw_path)])
    cmd.extend([
        *fc_out, "-map", map_label, "-map", "0:a?", "-shortest",
        # FINAL EXPORT — uploaded verbatim (YouTube copy + outro append are both
        # -c copy, so this bitstream is exactly what goes up). Max practical
        # 1440p quality for overlay content (text/UI/keyboard-cam edges are the
        # most banding/ringing-prone): CQ 15 with a 60M cap. Well below
        # YouTube's own re-encode so their lossy pass has clean input, but not
        # so low that the cap clips on busy motion (a too-low maxrate would just
        # become capped VBR, silently raising the QP in busy scenes). p7 =
        # highest-quality nvenc preset. If upload size ever matters more than
        # edge quality, drop toward CQ 18/35M; if graphics still shimmer, step
        # to CQ 14/80M.
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "15",
        "-maxrate", "60M", "-bufsize", "120M",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "256k",
        "-af", "asetpts=PTS-STARTPTS",
        "-movflags", "+faststart",
        "-g", "60", "-keyint_min", "60",
        "-f", "mp4", str(tmp_path),
    ])
    _log(f"  [ffmpeg] nvenc preset p7 cq 15 maxrate 60M (final export -> uploaded verbatim)")
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




def _remux_source_audio(overlay_path: Path, source_path: Path) -> None:
    """Replace the overlay video's audio with the original source audio.

    The overlay re-encodes audio per batch and stream-copies the batches
    together, which lets audio drift a few ms per batch (video is frame
    quantized, audio is sample-precise) — cumulative A/V desync. The overlay
    adds no audio, so the source's audio is the correct sync reference. Mux it
    back, trimmed to the overlay video's duration, stream-copying the video.
    """
    from overlay._common import _log
    tmp = overlay_path.with_name(overlay_path.name + ".resync.mp4")
    tmp.unlink(missing_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(overlay_path),
        "-i", str(source_path),
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        "-shortest",
        str(tmp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not tmp.is_file():
        _log(f"[ERROR] audio resync failed: {r.stderr[-500:]}")
        tmp.unlink(missing_ok=True)
        sys.exit(1)
    os.replace(tmp, overlay_path)


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


