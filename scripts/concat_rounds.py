from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"

_BATCH_RE = re.compile(r"batch-(\d+)-(\d+)\.mp4$")


def _concat_two(a: Path, b: Path, out: Path) -> None:
    a_mb = a.stat().st_size / 1024 / 1024
    b_mb = b.stat().st_size / 1024 / 1024
    print(f"\n  [Concat] {a_mb:.0f} MB + {b_mb:.0f} MB (disk I/O, no re-encode)...", end=" ", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        lst = Path(tmp) / "files.txt"
        with open(lst, "w") as f:
            f.write(f"file '{a.resolve()}'\n")
            f.write(f"file '{b.resolve()}'\n")
        r = subprocess.run(
            ["ffmpeg", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", str(out)],
            capture_output=True, text=True, timeout=3600,
        )
        if r.returncode != 0:
            print(f"\n  [CONCAT FAILED] {r.stderr[-300:]}")
            sys.exit(1)
    out_mb = out.stat().st_size / 1024 / 1024
    print(f"OK ({out_mb:.0f} MB)")


def _parse_batches(folder: Path, combined_exists: bool = False) -> tuple[list[Path], int]:
    files = sorted(
        [f for f in folder.glob("batch-*.mp4") if _BATCH_RE.match(f.name)],
        key=lambda f: int(_BATCH_RE.match(f.name).group(1)),
    )
    if not files:
        raise FileNotFoundError(f"No batch-*.mp4 files in {folder}")
    # Use first batch's start as expected (allows partial batches like rounds 20-21)
    expected_start = int(_BATCH_RE.match(files[0].name).group(1))
    for f in files:
        m = _BATCH_RE.match(f.name)
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            raise ValueError(f"Invalid batch range (start > end): {f.name}")
        if start < expected_start:
            raise ValueError(
                f"CONCAT_BATCH_OVERLAP: batch {f.name} starts at round {start} "
                f"but expected round {expected_start} (overlap with previous batch)"
            )
        if start > expected_start:
            msg = (
                f"remaining batches not contiguous (expected {expected_start}, "
                f"got {f.name})"
            ) if combined_exists else (
                f"expected batch starting at round {expected_start}, "
                f"got {f.name} (check for missing or overlapping batches)"
            )
            raise ValueError(f"CONCAT_BATCH_GAP: {msg}")
        expected_start = end + 1
    return files, int(_BATCH_RE.match(files[0].name).group(1))


_SEQ_RE = re.compile(r"sequence-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")


def _parse_sequence_files(folder: Path, batch_files: list[Path]) -> dict | None:
    """Parse sequence-*-tick-START-to-END.mp4 files left by CSDM.

    Maps sequence index -> round number within the batch (in render order).
    Returns {per_round_ticks, per_round_durations} or None if no sequences.
    """
    seqs = []
    for f in folder.glob("sequence-*-tick-*-to-*.mp4"):
        m = _SEQ_RE.match(f.name)
        if not m:
            continue
        seqs.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), f))
    if not seqs:
        return None
    seqs.sort(key=lambda x: x[0])

    # Distribute sequence files across rounds in the batch_files order.
    # CSDM writes one sequence file per rendered round; we pair them in order
    # with the round ranges spanned by the batch files.
    round_nums: list[int] = []
    for b in batch_files:
        m = _BATCH_RE.match(b.name)
        if not m:
            continue
        for rn in range(int(m.group(1)), int(m.group(2)) + 1):
            round_nums.append(rn)
    if len(seqs) != len(round_nums):
        print(
            f"  [warn] sequence files ({len(seqs)}) != rounds in batches ({len(round_nums)}); "
            f"keeping even-split durations"
        )
        return None

    per_round_ticks: dict[int, tuple[int, int]] = {}
    per_round_durations: dict[int, float] = {}
    for (seq_idx, start_tick, end_tick, f), rn in zip(seqs, round_nums):
        per_round_ticks[rn] = (start_tick, end_tick)
        per_round_durations[rn] = _probe_duration(f)
    print(
        f"  [seq] {len(seqs)} sequence files parsed: "
        f"{sum(per_round_durations.values()):.2f}s total across rounds"
    )
    return {"per_round_ticks": per_round_ticks, "per_round_durations": per_round_durations}


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0.0
    data = json.loads(r.stdout)
    return float(data.get("format", {}).get("duration", 0))


def concat_rounds(folder: Path) -> Path:
    combined = folder / "combined.mp4"
    files, _ = _parse_batches(folder, combined_exists=combined.exists())
    total_rounds = sum(
        int(_BATCH_RE.match(f.name).group(2)) - int(_BATCH_RE.match(f.name).group(1)) + 1
        for f in files
    )

    print(f"Concatenating {len(files)} batch(es) ({total_rounds} rounds) -> {combined}")

    round_offsets = {}  # round_num -> start_seconds
    batch_offsets: list[dict] = []
    cumulative = 0.0

    for f in files:
        m = _BATCH_RE.match(f.name)
        s, e = int(m.group(1)), int(m.group(2))

        if not combined.exists():
            f.rename(combined)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} -> {combined.name} ({mb:.0f} MB)")
        else:
            tmp = folder / "_tmp.mp4"
            _concat_two(combined, f, tmp)
            tmp.replace(combined)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} appended ({mb:.0f} MB)")

        # Probe batch duration, distribute evenly across rounds in batch
        dur = _probe_duration(combined if not f.is_file() else f)
        per_round = dur / (e - s + 1)
        for r in range(s, e + 1):
            offset = cumulative + (r - s) * per_round
            round_offsets[r] = offset
        cumulative += dur
        batch_offsets.append({
            "batch": f.name,
            "round_start": s,
            "round_end": e,
            "duration_seconds": dur,
        })

    # Write round offset sidecar. When CSDM sequence files are present, parse
    # the actual per-round tick ranges and probed video durations from them —
    # this is ground truth (avoids concat's even-split approximation when
    # rounds have unequal freeze + play + death spans). Falls back to the
    # even-split `per_round` estimate above when sequences are absent.
    seq_fields = _parse_sequence_files(folder, files)
    if seq_fields:
        # Replace per-round durations with probed sequence durations.
        per_round_ticks = seq_fields["per_round_ticks"]
        per_round_durations = seq_fields["per_round_durations"]
        cumulative = 0.0
        for rn in sorted(per_round_durations.keys()):
            round_offsets[rn] = cumulative
            cumulative += per_round_durations[rn]
        # Rewrite batch durations so they sum to the new cumulative total.
        for b in batch_offsets:
            rn_start, rn_end = b["round_start"], b["round_end"]
            b["duration_seconds"] = sum(
                per_round_durations[r] for r in range(rn_start, rn_end + 1)
                if r in per_round_durations
            )

    offset_path = folder / "combined.round_offsets.json"
    payload = {
        "total_rounds": total_rounds,
        "total_duration_seconds": cumulative,
        "round_offsets": {str(k): round(v, 3) for k, v in round_offsets.items()},
        "batches": batch_offsets,
    }
    if seq_fields:
        payload["per_round_ticks"] = {
            str(k): list(v) for k, v in seq_fields["per_round_ticks"].items()
        }
        payload["per_round_durations"] = {
            str(k): round(v, 3) for k, v in seq_fields["per_round_durations"].items()
        }
    with open(offset_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nRound offsets: {offset_path} ({len(round_offsets)} rounds)")

    print(f"\nDone. {combined} ({combined.stat().st_size // 1024 // 1024} MB)")
    return combined


def _get_resolution(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0, 0
    data = json.loads(r.stdout)
    s = data.get("streams", [])
    return (s[0]["width"], s[0]["height"]) if s else (0, 0)


def _is_valid_video(path: Path) -> bool:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0


def _upscale(src: Path, dst: Path, w: int, h: int) -> None:
    src_mb = src.stat().st_size / 1024 / 1024
    print(f"\n  [Upscale] {src_mb:.0f} MB -> {w}x{h} (GPU CUDA Lanczos + NVENC)...", end=" ", flush=True)
    temp = dst.with_suffix(".temp.mp4")
    t0 = time.time()
    cmd = [
        "ffmpeg", "-y", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(src),
        "-vf", f"scale_cuda={w}:{h}:interp_algo=lanczos,hwdownload,format=nv12",
        "-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr_hq", "-cq", "18", "-b:v", "0", "-maxrate", "50M",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "5.1",
        "-c:a", "copy", str(temp),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    elapsed = time.time() - t0
    if r.returncode != 0:
        temp.unlink(missing_ok=True)
        print(f"\n[ERROR] Upscale failed: {r.stderr[-300:]}")
        print("  [Fallback] Retrying with CPU Lanczos...")
        cmd[5] = f"scale={w}:{h}:flags=lanczos"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode != 0:
            temp.unlink(missing_ok=True)
            print(f"\n[ERROR] Upscale fallback also failed: {r.stderr[-300:]}")
            sys.exit(1)
    temp.replace(dst)
    mb = dst.stat().st_size / 1024 / 1024
    print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Concatenate round clips into one video")
    parser.add_argument("folder", help="Folder containing batch-*.mp4 files")
    parser.add_argument("--output", "-o", help="Output path (default: folder/combined.mp4)")
    parser.add_argument("--width", type=int, default=2560, help="Target width for upscale")
    parser.add_argument("--height", type=int, default=1440, help="Target height for upscale")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"[ERROR] Folder not found: {folder}")
        sys.exit(1)

    try:
        combined = concat_rounds(folder)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # Upscale to target resolution if needed (VP9 trick for YouTube)
    vid_w, vid_h = _get_resolution(combined)
    if vid_h < args.height:
        upscaled = folder / "_upscaled.mp4"
        if upscaled.exists() and not _is_valid_video(upscaled):
            print(f"\n  [Cleanup] Removing corrupt {upscaled.name}")
            upscaled.unlink()
        if not upscaled.exists():
            _upscale(combined, upscaled, args.width, args.height)
        upscaled.replace(combined)
    else:
        print(f"\n  [Skip upscale] Already {vid_w}x{vid_h}")

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        combined.replace(output)

    mb = combined.stat().st_size / 1024 / 1024
    print(f"\nDone. {combined} ({mb:.0f} MB)")


if __name__ == "__main__":
    main()
