"""
Render POV rounds in batches. 0 = all at once, N > 0 = N rounds per batch.
Handles split demos (p1, p2) automatically.
Batch mode uses incremental concat (never >2 inputs at once).

Usage:
    python scripts/render_pov.py <demo_path> <steam_id> [--batches 0] [--minimize-cs2]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root is on sys.path for imports like cs2_minimizer
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from cs2_minimizer import CS2Minimizer

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"

BASE_FLAGS = [
    "--mode", "player",
    "--perspective", "player",
    "--no-show-x-ray",
    "--no-show-only-death-notices",
    "--show-assists",
    "--record-audio",
    "--concatenate-sequences",
    "--ffmpeg-video-codec", "libx264",
    "--ffmpeg-crf", "15",
    "--ffmpeg-output-parameters=-profile:v high -pix_fmt yuv420p -level 5.1 -minrate 20M -maxrate 40M -bufsize 40M",
    "--recording-system", "CS",
    "--close-game-after-recording",
    "--cfg", "assets/cs2_pov.cfg",
]


def find_demo_parts(demo_path: str) -> list[str]:
    path = Path(demo_path)
    if not path.exists():
        print(f"[ERROR] Demo not found: {path}")
        sys.exit(1)
    parts: list[Path] = [path]
    m = re.search(r"(.*)-p(\d+)(\.dem)$", path.name, re.IGNORECASE)
    if m:
        base = m.group(1)
        ext = m.group(3)
        folder = path.parent
        for f in sorted(folder.glob(f"{base}-p*{ext}")):
            if f not in parts:
                parts.append(f)
        parts.sort(key=lambda p: int(re.search(r"-p(\d+)", p.stem).group(1)))
    return [str(p) for p in parts]


def get_round_count(demo_path: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [CSDM, "json", demo_path, "--output-folder", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if "unknown demo source" in (r.stderr or "").lower():
            cmd += ["--source", "challengermode"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return 0
        jf = list(Path(tmp).glob("*.json"))
        if not jf:
            return 0
        data = json.loads(jf[0].read_text())
        return len(data.get("rounds", []))


def concat_two(a: Path, b: Path, out: Path) -> None:
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


def run_csdm(cmd: list[str], label: str) -> Path | None:
    print(f"  [{label}]...", end=" ", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    err = (result.stderr or "") + (result.stdout or "")

    if "unknown demo source" in err.lower() and "--source" not in cmd:
        cmd += ["--source", "challengermode"]
        return run_csdm(cmd, f"{label} (challengermode)")

    elapsed = time.time() - t0

    if "Steam is not running" in err:
        print("FAILED - Steam is not running.")
        sys.exit(1)

    # No sequences = player had no events in this round, non-fatal
    no_seqs = "no sequences generated" in err.lower()

    if result.returncode != 0 and not no_seqs:
        print(f"FAILED ({elapsed:.0f}s, exit {result.returncode})")
        print(err[-500:])
        sys.exit(1)

    # Find the newest mp4 in the output directory
    for i, a in enumerate(cmd):
        if a == "--output" and i + 1 < len(cmd):
            out_dir = Path(cmd[i + 1])
            break
    else:
        print("FAILED (no output dir in cmd)")
        sys.exit(1)

    mp4s = list(out_dir.rglob("*.mp4"))
    if mp4s:
        vid = max(mp4s, key=lambda p: p.stat().st_mtime)
        mb = vid.stat().st_size / 1024 / 1024
        print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")
        if mb < 1:
            print("[ERROR] Video too small")
            sys.exit(1)
        return vid

    if no_seqs:
        print("OK (no sequences)")
        return None

    print(f"FAILED ({elapsed:.0f}s, no video)")
    sys.exit(1)


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


def _upscale(src: Path, dst: Path, w: int, h: int) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale={w}:{h}:flags=lanczos",
        "-c:v", "libx264", "-crf", "15",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "5.1",
        "-minrate", "20M", "-maxrate", "40M", "-bufsize", "40M",
        "-preset", "slow", "-c:a", "copy", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        print(f"[ERROR] Upscale failed: {r.stderr[-300:]}")
        sys.exit(1)
    mb = dst.stat().st_size / 1024 / 1024
    print(f"  [OK] {dst.name} ({mb:.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render POV rounds")
    parser.add_argument("demo", help="Path to .dem file")
    parser.add_argument("steam_id", help="Steam64 ID")
    parser.add_argument("--output", "-o", help="Output folder")
    parser.add_argument("--batches", type=int, default=0,
                        help="Rounds per batch (0 = all at once)")
    parser.add_argument("--framerate", type=int, default=60)
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--minimize-cs2", action="store_true",
                        help="Auto-minimize CS2 when it launches (prevents focus stealing)")
    args = parser.parse_args()

    parts = find_demo_parts(args.demo)
    print(f"Found {len(parts)} demo part(s):")
    for p in parts:
        print(f"  {Path(p).name}")

    if args.output:
        output_dir = Path(args.output)
    else:
        stem = Path(parts[0]).stem.replace("-p1", "").replace(".dem", "")
        output_dir = Path("demos/renders") / f"pov-{stem}"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output:  {output_dir}")

    combined = output_dir / "combined.mp4"
    if combined.exists():
        print(f"  [SKIP] {combined.name} ({combined.stat().st_size // 1024 // 1024} MB)")
        return

    minimizer = None
    if args.minimize_cs2:
        minimizer = CS2Minimizer()
        minimizer.start()
        print("CS2 auto-minimize enabled (won't steal focus)")

    batch_idx = 0
    global_offset = 0

    for pi, part in enumerate(parts):
        n_rounds = get_round_count(part)
        part_name = Path(part).name
        print(f"\n--- {part_name}: {n_rounds} round(s) ---")
        if n_rounds == 0:
            continue

        if args.batches > 0:
            round_nums = list(range(1, n_rounds + 1))
            for b_start in range(0, n_rounds, args.batches):
                batch = round_nums[b_start:b_start + args.batches]
                batch_str = ",".join(str(r) for r in batch)
                global_rounds = [global_offset + r for r in batch]
                label = f"r{global_rounds[0]}-{global_rounds[-1]}"
                batch_out = output_dir / f"_batch{batch_idx}.mp4"

                cmd = [
                    CSDM, "video", part,
                    "--steamids", args.steam_id,
                    "--event", "rounds",
                    "--rounds", batch_str,
                    "--output-file-name", batch_out.name,
                    "--output", str(output_dir),
                    "--framerate", str(args.framerate),
                    "--width", str(args.width),
                    "--height", str(args.height),
                ] + BASE_FLAGS

                vid = run_csdm(cmd, label)

                if vid is None:
                    batch_idx += 1
                    continue

                # csdm may write elsewhere; ensure it's at batch_out
                if vid != batch_out:
                    vid.replace(batch_out)

                # Incremental concat: merge batch into combined
                if not combined.exists():
                    batch_out.rename(combined)
                    mb = combined.stat().st_size / 1024 / 1024
                    print(f"    -> {combined.name} ({mb:.0f} MB)")
                else:
                    tmp = output_dir / "_tmp.mp4"
                    print(f"    -> appending to {combined.name}...", end=" ", flush=True)
                    t0 = time.time()
                    concat_two(combined, batch_out, tmp)
                    tmp.replace(combined)
                    batch_out.unlink(missing_ok=True)
                    elapsed = time.time() - t0
                    mb = combined.stat().st_size / 1024 / 1024
                    print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")

                batch_idx += 1
        else:
            cmd = [
                CSDM, "video", part,
                "--steamids", args.steam_id,
                "--event", "rounds",
                "--output-file-name", "combined.mp4",
                "--output", str(output_dir),
                "--framerate", str(args.framerate),
                "--width", str(args.width),
                "--height", str(args.height),
            ] + BASE_FLAGS

            vid = run_csdm(cmd, "all rounds")
            if vid != combined:
                vid.replace(combined)

        global_offset += n_rounds

    if minimizer:
        minimizer.stop()

    if not combined.exists():
        print("[ERROR] No video produced")
        sys.exit(1)

    # csdm --recording-system CS captures at game resolution, ignoring --width/--height.
    # Upscale to 1440p so YouTube allocates VP9 codec (sharper even at 1080p playback).
    vid_w, vid_h = _get_resolution(combined)
    if vid_h < args.height:
        upscaled = output_dir / "_upscaled.mp4"
        print(f"\n  [Upscale] {vid_w}x{vid_h} -> {args.width}x{args.height} (VP9 trigger)...")
        _upscale(combined, upscaled, args.width, args.height)
        upscaled.replace(combined)

    print(f"\nDone. Output: {combined} ({combined.stat().st_size // 1024 // 1024} MB)")


if __name__ == "__main__":
    main()
