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


def _parse_batches(folder: Path, combined_exists: bool = False) -> list[Path]:
    files = sorted(
        [f for f in folder.glob("batch-*.mp4") if _BATCH_RE.match(f.name)],
        key=lambda f: int(_BATCH_RE.match(f.name).group(1)),
    )
    if not files:
        raise FileNotFoundError(f"No batch-*.mp4 files in {folder}")
    expected_start = 1 if not combined_exists else int(_BATCH_RE.match(files[0].name).group(1))
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
    return files


def concat_rounds(folder: Path) -> Path:
    combined = folder / "combined.mp4"
    files = _parse_batches(folder, combined_exists=combined.exists())
    total_rounds = sum(
        int(_BATCH_RE.match(f.name).group(2)) - int(_BATCH_RE.match(f.name).group(1)) + 1
        for f in files
    )

    print(f"Concatenating {len(files)} batch(es) ({total_rounds} rounds) -> {combined}")

    for f in files:
        if not combined.exists():
            f.rename(combined)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} -> {combined.name} ({mb:.0f} MB)")
        else:
            tmp = folder / "_tmp.mp4"
            _concat_two(combined, f, tmp)
            tmp.replace(combined)
            f.unlink(missing_ok=True)
            mb = combined.stat().st_size / 1024 / 1024
            print(f"  {f.name} appended ({mb:.0f} MB)")

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
        "-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr_hq", "-cq", "15", "-b:v", "0", "-maxrate", "50M",
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
