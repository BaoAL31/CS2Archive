from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from cs2_minimizer import CS2Minimizer

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"

FFMPEG_PATH = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"

BASE_FLAGS = [
    "--mode", "player",
    "--perspective", "player",
    "--no-show-x-ray",
    "--no-show-only-death-notices",
    "--show-assists",
    "--record-audio",
    "--concatenate-sequences",
    "--ffmpeg-executable-path", FFMPEG_PATH,
    "--ffmpeg-video-codec", "h264_nvenc",
    "--ffmpeg-output-parameters=-rc vbr_hq -b:v 0 -cq 18 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
    "--recording-system", "HLAE",
    "--close-game-after-recording",
]


def resolve_output_dir(output: str | None, first_demo_path: str, steam_id: str) -> Path:
    """HLAE mirv_streams must receive an absolute --output (relative paths fail under the game cwd)."""
    if output:
        path = Path(output)
    else:
        stem = Path(first_demo_path).stem.replace("-p1", "").replace(".dem", "")
        path = _PROJECT_ROOT / "demos" / "renders" / f"pov-{stem}_{steam_id}"
    return path.resolve()


def abs_cfg_path() -> Path:
    return (_PROJECT_ROOT / "assets" / "cs2_pov.cfg").resolve()


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
        data = json.loads(jf[0].read_text(encoding="utf-8"))
        return len(data.get("rounds", []))


def run_csdm(cmd: list[str], label: str, expected: Path | None = None) -> Path | None:
    print(f"  [{label}]...", end=" ", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    err = (result.stderr or "") + (result.stdout or "")

    if "unknown demo source" in err.lower() and "--source" not in cmd:
        cmd += ["--source", "challengermode"]
        return run_csdm(cmd, f"{label} (challengermode)", expected)

    elapsed = time.time() - t0

    if "Steam is not running" in err:
        print("FAILED - Steam is not running.")
        sys.exit(1)

    if "Raw files not found" in err:
        print("FAILED - HLAE produced no video (check absolute --output; see AGENTS.md).")
        sys.exit(1)

    if result.returncode != 0:
        print(f"FAILED ({elapsed:.0f}s, exit {result.returncode})")
        print(err[-500:])
        sys.exit(1)

    if expected is not None and expected.exists():
        mb = expected.stat().st_size / 1024 / 1024
        if mb < 1:
            print("[ERROR] Video too small")
            sys.exit(1)
        print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")
        return expected

    # Fallback: find most recent mp4 in output dir
    for i, a in enumerate(cmd):
        if a == "--output" and i + 1 < len(cmd):
            out_dir = Path(cmd[i + 1])
            break
    else:
        print("FAILED (no output dir in cmd)")
        sys.exit(1)

    mp4s = [p for p in out_dir.rglob("*.mp4") if p.name != "combined.mp4"]
    if mp4s:
        vid = max(mp4s, key=lambda p: p.stat().st_mtime)
        if expected is not None and vid != expected:
            for _ in range(10):
                try:
                    shutil.copy2(str(vid), str(expected))
                    vid = expected
                    break
                except PermissionError:
                    time.sleep(1)
            else:
                print("FAILED (could not copy video, file locked)")
                sys.exit(1)
        mb = vid.stat().st_size / 1024 / 1024
        print(f"OK ({elapsed:.0f}s, {mb:.0f} MB)")
        if mb < 1:
            print("[ERROR] Video too small")
            sys.exit(1)
        return vid

    print(f"FAILED ({elapsed:.0f}s, no video)")
    if err.strip():
        print(err[-800:])
    sys.exit(1)


def _get_player_crosshair(steam_id: str, demo_parts: list[str]) -> list[str]:
    """Extract player crosshair from demo analysis JSON via csdm json export."""
    for p in demo_parts:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [CSDM, "json", p, "--output-folder", tmp]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                continue
            jf = list(Path(tmp).glob("*.json"))
            if not jf:
                continue
            data = json.loads(jf[0].read_text(encoding="utf-8"))
            for pl in data.get("players", []):
                if pl.get("steamId") == steam_id:
                    code = pl.get("crosshairShareCode")
                    if code:
                        from crosshair_code import decode_crosshair, crosshair_to_convars
                        cvars = crosshair_to_convars(decode_crosshair(code))
                        return ["crosshair 1"] + cvars
    return []

def _write_cfg_with_crosshair(base_cfg: Path, crosshair_cmds: list[str], output: Path) -> None:
    """Write cs2_pov.cfg with crosshair commands appended."""
    output.write_text(base_cfg.read_text(encoding="utf-8"), encoding="utf-8")
    if crosshair_cmds:
        with open(output, "a", encoding="utf-8") as f:
            f.write("\n// Player crosshair from demo\n")
            for cmd in crosshair_cmds:
                f.write(cmd + "\n")

def _check_nvenc() -> None:
    """Verify h264_nvenc actually works before starting render."""
    import subprocess, time
    cmd = [FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "color=c=red:s=2560x1440:d=1",
           "-c:v", "h264_nvenc", "-rc", "vbr_hq", "-b:v", "0", "-cq", "15",
           "-preset", "p7", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        err = r.stderr.lower()
        if "unknown encoder" in err or "h264_nvenc" in err:
            print("[FATAL] h264_nvenc not available in ffmpeg. Install NVIDIA GPU drivers + NVENC ffmpeg.")
        elif "driver" in err or "cuda" in err:
            print("[FATAL] NVIDIA driver issue. Update GPU drivers.")
        else:
            print(f"[FATAL] NVENC test failed: {r.stderr[-300:]}")
        sys.exit(1)
    print("  [OK] NVENC encoder verified")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render POV rounds in batches")
    parser.add_argument("demo", help="Path to .dem file")
    parser.add_argument("steam_id", help="Steam64 ID")
    parser.add_argument("--output", "-o", help="Output folder")
    parser.add_argument("--framerate", type=int, default=60)
    parser.add_argument("--width", type=int, default=2560)
    parser.add_argument("--height", type=int, default=1440)
    parser.add_argument("--batches", type=int, default=10,
                        help="Rounds per batch (default: 10). Each batch produces one MP4.")
    parser.add_argument("--no-minimize-cs2", action="store_true",
                        help="Disable auto-minimize CS2 when it launches (default: enabled)")
    args = parser.parse_args()

    _check_nvenc()

    parts = find_demo_parts(args.demo)
    print(f"Found {len(parts)} demo part(s):")
    for p in parts:
        print(f"  {Path(p).name}")

    output_dir = resolve_output_dir(args.output, parts[0], args.steam_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = abs_cfg_path()

    crosshair_cmds = _get_player_crosshair(args.steam_id, parts)
    if crosshair_cmds:
        print(f"  Player crosshair extracted ({len(crosshair_cmds)} cvars)")
    else:
        print("  [WARN] No crosshair found in demo — rendering with default CS2 crosshair")

    print(f"Output:  {output_dir}")
    print(f"Batch size: {args.batches} round(s) per batch")

    if args.batches < 1:
        print("[ERROR] --batches must be >= 1")
        sys.exit(1)

    minimizer = None
    if not args.no_minimize_cs2:
        minimizer = CS2Minimizer()
        minimizer.start()
        print("CS2 auto-minimize enabled (won't steal focus)")

    global_round = 0
    total_rendered = 0
    expected_batches: list[str] = []

    for part in parts:
        n_rounds = get_round_count(part)
        part_name = Path(part).name
        print(f"\n--- {part_name}: {n_rounds} round(s) ---")
        if n_rounds == 0:
            continue

        batch_size = args.batches
        for local_start in range(1, n_rounds + 1, batch_size):
            local_end = min(local_start + batch_size - 1, n_rounds)
            global_start = global_round + local_start
            global_end = global_round + local_end

            out_name = f"batch-{global_start:03d}-{global_end:03d}"
            expected_batches.append(out_name + ".mp4")
            out_path = output_dir / (out_name + ".mp4")

            if out_path.exists() and out_path.stat().st_size >= 1_048_576:
                mb = out_path.stat().st_size / 1024 / 1024
                print(f"  [SKIP] {out_name}.mp4 exists ({mb:.0f} MB)")
                total_rendered += local_end - local_start + 1
                continue

            local_rounds = list(range(local_start, local_end + 1))
            # Write cfg with crosshair injected (csdm reads --cfg, not the .dem.json)
            if crosshair_cmds:
                render_cfg = output_dir / "render_crosshair.cfg"
                _write_cfg_with_crosshair(cfg, crosshair_cmds, render_cfg)
                cfg_to_use = render_cfg
                print(f"  Crosshair cfg: {render_cfg}")
            else:
                cfg_to_use = cfg
            cmd = [
                CSDM, "video", str(Path(part).resolve()),
                "--steamids", args.steam_id,
                "--event", "rounds",
                "--rounds", ",".join(str(r) for r in local_rounds),
                "--output-file-name", out_name,
                "--output", str(output_dir),
                "--framerate", str(args.framerate),
                "--width", str(args.width),
                "--height", str(args.height),
                "--cfg", str(cfg_to_use),
            ] + BASE_FLAGS

            vid = run_csdm(cmd, out_name, expected=out_path)

            if vid is None:
                continue

            total_rendered += local_end - local_start + 1

        global_round += n_rounds

    if minimizer:
        minimizer.stop()

    if total_rendered == 0:
        print("[ERROR] No rounds rendered")
        sys.exit(1)

    # Validate all expected batch files exist
    missing = [n for n in expected_batches if not (output_dir / n).exists()]
    if missing:
        print(f"[ERROR] Missing expected batch files: {missing}")
        sys.exit(1)

    print(f"\nDone. {total_rendered} round(s) in {len(expected_batches)} batch(es) at {output_dir}")


if __name__ == "__main__":
    main()
