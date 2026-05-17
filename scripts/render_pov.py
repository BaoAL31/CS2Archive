"""
Render each round of a player's POV individually, one at a time.
Handles split demos (p1, p2) automatically.

Usage:
    python scripts/render_pov.py <demo_path> <steam_id> [--output <folder>] [--rounds 1-30] [--framerate 60]

Examples:
    python scripts/render_pov.py "demos/hltv/faze-vs-vitality/nuke.dem" 76561197991272318
    python scripts/render_pov.py "demos/hltv/faze-vs-vitality/nuke-p2.dem" 76561197991272318 --rounds 2-29
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP_DIR = Path(os.environ.get("TMPDIR", "C:/Users/jembo/AppData/Local/Temp/opencode"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

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
    "--ffmpeg-crf", "18",
    "--ffmpeg-output-parameters=-profile:v high -pix_fmt yuv420p -level 4.2",
    "--recording-system", "CS",
    "--close-game-after-recording",
    "--cfg", "assets/cs2_pov.cfg",
]


def find_demo_parts(demo_path: str) -> list[str]:
    """Find all parts of a split demo (p1, p2, ...) in the same directory."""
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
    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmp:
        result = subprocess.run(
            [CSDM, "json", demo_path, "--output-folder", tmp],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            print(f"[ERROR] csdm json failed: {result.stderr}")
            return 0
        json_files = list(Path(tmp).glob("*.json"))
        if not json_files:
            return 0
        data = json.loads(json_files[0].read_text())
        return len(data.get("rounds", []))


def parse_round_range(s: str) -> list[int]:
    if not s:
        return []
    parts = s.split(",")
    result = []
    for p in parts:
        p = p.strip()
        if "-" in p:
            a, b = p.split("-")
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(p))
    return sorted(set(result))


def render_round(
    demo_path: str,
    steam_id: str,
    round_num: int,
    part_round_offset: int,
    output_dir: Path,
    framerate: int,
    width: int,
    height: int,
) -> bool:
    global_round = part_round_offset + round_num
    out = output_dir / f"round-{global_round:03d}.mp4"
    if out.exists():
        print(f"  [SKIP] round {global_round} already exists")
        return True

    cmd = [
        CSDM, "video", demo_path,
        "--steamids", steam_id,
        "--event", "rounds",
        "--rounds", str(round_num),
        "--output", str(output_dir),
        "--framerate", str(framerate),
        "--width", str(width),
        "--height", str(height),
    ] + BASE_FLAGS

    print(f"  [RENDER] round {global_round} (part r{round_num})...", end=" ", flush=True)
    t0 = time.time()

    print(f"  csdm cmd: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=900,
    )

    elapsed = time.time() - t0

    err = (result.stderr or "") + (result.stdout or "")
    print(f"  csdm stdout tail: {(result.stdout or '')[-500:]}")
    print(f"  csdm stderr tail: {(result.stderr or '')[-500:]}")
    if "Steam is not running" in err:
        print("FAILED - Steam is not running. Start Steam and CS2, then re-run.")
        sys.exit(1)

    if result.returncode != 0:
        print(f"FAILED ({elapsed:.0f}s, exit code {result.returncode})")
        return False

    rendered = sorted(output_dir.glob("sequence-*.mp4"), key=lambda p: p.stat().st_mtime)
    if rendered:
        latest = rendered[-1]
        if latest.exists():
            if out.exists():
                out.unlink()
            latest.rename(out)
            mb = out.stat().st_size / 1024 / 1024
            print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")
            return True

    no_video_count = getattr(render_round, "no_video_count", 0) + 1
    render_round.no_video_count = no_video_count
    print(f"OK ({elapsed:.0f}s, no video)")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Render POV rounds one at a time (handles p1/p2)")
    parser.add_argument("demo", help="Path to .dem file or any part (p1, p2, ...)")
    parser.add_argument("steam_id", help="Steam64 ID of the player")
    parser.add_argument("--output", "-o", help="Output folder (default: auto from demo name)")
    parser.add_argument("--rounds", default="", help="Round range like 1-29 or 1,2,5-10 (default: all)")
    parser.add_argument("--framerate", type=int, default=60, help="Output framerate (default: 60)")
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1440)
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
    print()

    user_rounds: list[int] | None = None
    if args.rounds:
        user_rounds = parse_round_range(args.rounds)

    render_round.no_video_count = 0
    total_rounds = 0
    failed_rounds = 0
    global_offset = 0
    for pi, part in enumerate(parts):
        part_name = Path(part).name
        total = get_round_count(part)
        print(f"\n--- {part_name}: {total} round(s) ---")

        if total == 0:
            continue

        if user_rounds:
            part_rounds = [r - global_offset for r in user_rounds
                           if global_offset < r <= global_offset + total]
        else:
            part_rounds = list(range(1, total + 1))

        if not part_rounds:
            print("  (no rounds in this part)")
            global_offset += total
            continue

        for r in part_rounds:
            ok = render_round(
                part, args.steam_id, r, global_offset,
                output_dir, args.framerate, args.width, args.height,
            )
            if not ok:
                failed_rounds += 1
            total_rounds += 1

        global_offset += total

    print(f"\nDone. {total_rounds} round(s), {failed_rounds} failed, {render_round.no_video_count} no-video")
    if total_rounds > 0 and failed_rounds == total_rounds:
        print("[ERROR] All rounds failed to produce video. This likely means the player was dead or not found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
