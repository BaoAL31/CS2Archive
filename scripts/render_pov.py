from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
for _p in (str(_PROJECT_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cs2_minimizer import CS2Minimizer

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
FFMPEG_PATH = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
GAME_CFG = Path(r"D:\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg")
CFG_BACKUP_DIR = Path(r"D:\Projects\CS2UtilArchive\cs2_config_backup")

# Source-of-truth personal cfgs live OUTSIDE Steam folder (Steam flags them).
# Render writes to GAME_CFG/autoexec.cfg during rendering; finally restores from backup.
AUTOEXEC_RENDER = GAME_CFG / "autoexec_render.cfg"  # pre-render render-crosshair template
AUTOEXEC_PERSONAL = CFG_BACKUP_DIR / "autoexec_personal.cfg"
AUTOEXEC_PERSONAL_BACKUP = CFG_BACKUP_DIR / "autoexec_personal_backup.cfg"
AUTOEXEC_MAIN = GAME_CFG / "autoexec.cfg"  # active cfg CS2 reads on startup
RENDER_CROSSHAIR_CFG = GAME_CFG / "render_crosshair.cfg"

os.environ["HF_HOME"] = "D:/.cache/huggingface"
os.environ["HF_HUB_CACHE"] = "D:/.cache/huggingface/hub"

HF_REPO_DEFAULT = "cs2povarchive/cs2-demos"
HF_CACHE_DIR = Path(os.environ["HF_HOME"])


def _ensure_demo(demo_path_str: str, hf_root: str = "",
                 hf_repo: str = HF_REPO_DEFAULT,
                 match_slug: str | None = None,
                 match_id: str = "") -> str:
    """Return resolved demo path, downloading from HuggingFace if missing locally.

    match_slug: remote folder name on HF. Derives from parent folder if omitted.
    match_id:   HLTV match ID, prefixed to slug for HF remote path (e.g. "2395002").
    """
    path = Path(demo_path_str)
    if path.exists():
        return str(path.resolve())

    if not hf_root:
        print(f"[ERROR] Demo not found: {path}")
        print(f"[HINT]  Use --hf-root <root> (e.g. iem_cologne_major_2026) to auto-download from HF.")
        sys.exit(1)

    slug = match_slug if match_slug else path.parent.name
    folder = f"{match_id}-{slug}" if match_id else slug
    dem_filename = path.name
    hf_remote = f"{hf_root}/{folder}/{dem_filename}"
    path.parent.mkdir(parents=True, exist_ok=True)

    print(f"  [HF] Demo not found locally. Downloading from {hf_repo}...")
    print(f"       hf://{hf_repo}/{hf_remote}")
    print(f"       cache: {HF_CACHE_DIR / 'hub'}")
    try:
        from huggingface_hub import hf_hub_download
        cached = hf_hub_download(
            repo_id=hf_repo,
            filename=hf_remote,
            repo_type="dataset",
        )
        shutil.copy2(cached, path)
    except Exception as e:
        print(f"[ERROR] HF download failed: {e}")
        sys.exit(1)

    if not path.exists():
        print(f"[ERROR] Download said success but file not found: {path}")
        sys.exit(1)

    mb = path.stat().st_size / 1024 / 1024
    print(f"  [OK] Demo downloaded ({mb:.0f} MB): {path}")
    return str(path.resolve())


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
    "--ffmpeg-crf", "16",
    "--ffmpeg-output-parameters=-cq 16 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
    "--recording-system", "HLAE",
    "--close-game-after-recording",
]


def resolve_output_dir(output: str | None, first_demo_path: str, steam_id: str) -> Path:
    if output:
        path = Path(output)
    else:
        stem = Path(first_demo_path).stem.replace("-p1", "").replace(".dem", "")
        path = _PROJECT_ROOT / "renders" / f"pov-{stem}_{steam_id}"
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
                        return cvars
    return []


def _write_render_autoexec(cvars: list[str]) -> None:
    lines = ["crosshair 1", "cl_chatfilters 63", "snd_mvp_volume 0"] + cvars
    content = "\n".join(lines) + "\n"
    AUTOEXEC_RENDER.write_text(content, encoding="utf-8")
    RENDER_CROSSHAIR_CFG.write_text(content, encoding="utf-8")


def _swap_autoexec(src: Path) -> None:
    if src == AUTOEXEC_PERSONAL and not AUTOEXEC_PERSONAL.exists():
        if AUTOEXEC_PERSONAL_BACKUP.exists():
            src = AUTOEXEC_PERSONAL_BACKUP
        else:
            print(f"  [ERROR] No personal autoexec found at {AUTOEXEC_PERSONAL} or {AUTOEXEC_PERSONAL_BACKUP}")
            print(f"          CS2 will keep using the last-rendered crosshair cfg until you fix this.")
            return
    if src.exists():
        shutil.copy2(str(src), str(AUTOEXEC_MAIN))
    else:
        print(f"  [ERROR] Swap source missing: {src}")


def _kill_stale_processes() -> None:
    names = ["cs2.exe", "csdm.cmd", "csdm.exe", "HLAE.exe"]
    for n in names:
        try:
            subprocess.run(["taskkill", "/f", "/im", n], capture_output=True, timeout=10)
        except Exception:
            pass
    time.sleep(2)


def _check_nvenc() -> None:
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
    parser.add_argument("--rounds", type=str, default="",
                    help="Comma-separated list of specific rounds to render, e.g. '1,3,5' or '2-4,7'. If omitted, all rounds are rendered.")

    parser.add_argument("--no-minimize-cs2", action="store_true",
                        help="Disable auto-minimize CS2 when it launches (default: enabled)")
    parser.add_argument("--hf-root", default="",
                        help="HuggingFace dataset folder root (e.g. iem_cologne_major_2026). Auto-downloads demo if missing.")
    parser.add_argument("--hf-repo", default=HF_REPO_DEFAULT,
                        help=f"HuggingFace dataset repo (default: {HF_REPO_DEFAULT})")
    parser.add_argument("--match-slug", default=None,
                        help="Remote folder name on HF (derived from demo path parent if omitted).")
    parser.add_argument("--match-id", default="",
                        help="HLTV match ID for HF path prefix (e.g. 2395002). Required when hf_root set and local demo missing.")
    parser.add_argument("--overlay", action="store_true",
                        help="Apply input overlay + util cam trajectory after render.")
    args = parser.parse_args()

    _kill_stale_processes()
    _check_nvenc()

    demo_path = _ensure_demo(args.demo, args.hf_root, args.hf_repo, args.match_slug, args.match_id)
    parts = find_demo_parts(demo_path)
    print(f"Found {len(parts)} demo part(s):")
    for p in parts:
        print(f"  {Path(p).name}")

    output_dir = resolve_output_dir(args.output, parts[0], args.steam_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    cvars = _get_player_crosshair(args.steam_id, parts)
    if cvars:
        print(f"  Player crosshair extracted ({len(cvars)} cvars)")
        _write_render_autoexec(cvars)
        _swap_autoexec(AUTOEXEC_RENDER)
        print(f"  Swapped {AUTOEXEC_RENDER.name} -> {AUTOEXEC_MAIN.name}")
    else:
        print("  [WARN] No crosshair found in demo — keeping current autoexec.cfg")

    print(f"Output:  {output_dir}")
    print(f"Batch size: {args.batches} round(s) per batch")

    if args.batches < 1:
        print("[ERROR] --batches must be >= 1")
        sys.exit(1)

    try:
        minimizer = None
        if not args.no_minimize_cs2:
            minimizer = CS2Minimizer()
            minimizer.start()
            print("CS2 auto-minimize enabled (won't steal focus)")

        global_round = 0
        total_rendered = 0
        expected_batches: list[str] = []

        # Parse optional round selection
        if args.rounds:
            try:
                wanted = set()
                for part in args.rounds.split(','):
                    part = part.strip()
                    if not part:
                        continue
                    if '-' in part:
                        a, b = part.split('-')
                        start, end = int(a), int(b)
                        if start > end:
                            start, end = end, start
                        for v in range(start, end + 1):
                            if v >= 1:
                                wanted.add(v)
                    else:
                        v = int(part)
                        if v >= 1:
                            wanted.add(v)
            except Exception:
                sys.exit("[ERROR] Invalid --rounds format. Use comma-separated numbers or ranges like '1,3-5,7'.")
        else:
            wanted = None  # None means all rounds

        for part in parts:
            n_rounds = get_round_count(part)
            part_name = Path(part).name
            print(f"\n--- {part_name}: {n_rounds} round(s) ---")
            if n_rounds == 0:
                continue

            # Determine which local rounds (1-indexed within this part) are wanted
            all_local = list(range(1, n_rounds + 1))
            if wanted is not None:
                desired_local = [lr for lr in all_local if (global_round + lr) in wanted]
            else:
                desired_local = all_local

            if not desired_local:
                print(f"  [INFO] No requested rounds in this part.")
                global_round += n_rounds
                continue

            batch_size = args.batches
            for i in range(0, len(desired_local), batch_size):
                batch = desired_local[i:i + batch_size]
                local_start = batch[0]
                local_end = batch[-1]
                global_start = global_round + local_start
                global_end = global_round + local_end

                out_name = f"batch-{global_start:03d}-{global_end:03d}"
                expected_batches.append(out_name + ".mp4")
                out_path = output_dir / (out_name + ".mp4")

                if out_path.exists() and os.path.getsize(out_path) >= 1_048_576:
                    mb = os.path.getsize(out_path) / 1024 / 1024
                    print(f"  [SKIP] {out_name}.mp4 exists ({mb:.0f} MB)")
                    total_rendered += len(batch)
                    continue

                cmd = [
                    CSDM, "video", str(Path(part).resolve()),
                    "--steamids", args.steam_id,
                    "--event", "rounds",
                    "--rounds", ",".join(str(r) for r in batch),
                    "--output-file-name", out_name,
                    "--output", str(output_dir),
                    "--framerate", str(args.framerate),
                    "--width", str(args.width),
                    "--height", str(args.height),
                    "--cfg", str(abs_cfg_path()),
                ] + BASE_FLAGS

                vid = run_csdm(cmd, out_name, expected=out_path)

                if vid is None:
                    print(f"[ERROR] run_csdm returned None for {out_name} — no video produced")
                    sys.exit(1)

                total_rendered += len(batch)

            global_round += n_rounds

        if minimizer:
            minimizer.stop()

        if total_rendered == 0:
            print("[ERROR] No rounds rendered")
            sys.exit(1)

        missing = [n for n in expected_batches if not (output_dir / n).exists()]
        if missing:
            print(f"[ERROR] Missing expected batch files: {missing}")
            sys.exit(1)

        print(f"\nDone. {total_rendered} round(s) in {len(expected_batches)} batch(es) at {output_dir}")

        if args.overlay:
            # Run overlay on the rendered output
            vid = output_dir / expected_batches[0]
            if not vid.exists():
                print(f"[WARN] --overlay set but no video found: {vid}")
            else:
                round_arg = []
                if args.rounds:
                    # Use first round number for tick offset
                    r = args.rounds.split(",")[0].split("-")[0]
                    round_arg = ["--round", r]
                subprocess.run([
                    sys.executable, "scripts/overlay_pov.py",
                    "--video", str(vid),
                    "--demo", demo_path,
                    "--steam-id", args.steam_id,
                ] + round_arg, timeout=7200)
    finally:
        _swap_autoexec(AUTOEXEC_PERSONAL)
        print(f"  Swapped {AUTOEXEC_PERSONAL.name} -> {AUTOEXEC_MAIN.name} (restored)")


if __name__ == "__main__":
    main()
