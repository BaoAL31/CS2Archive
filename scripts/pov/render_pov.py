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

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

from cs2_minimizer import CS2Park

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
    # CSDM defaults to --no-player-voices, which pushes `voice_enable 0` during
    # the render (kills voice + talking indicators). Keep player voice on; the
    # in-game cfg (cl_mute_enemy_team 1) drops the enemy, so only the POV
    # team's comms + indicators land in the recorded audio.
    "--player-voices",
    # NOTE: --concatenate-sequences intentionally omitted. Without it CSDM
    # emits one sequence-{i}-tick-{A}-to-{B}.mp4 per round. We keep those files
    # so concat_rounds.py can read the real per-round tick spans and write
    # per_round_ticks (required for a synced overlay). The batch file is no
    # longer produced by CSDM; concat consumes the round-* files directly.
    "--ffmpeg-executable-path", FFMPEG_PATH,
    "--ffmpeg-video-codec", "h264_nvenc",
    "--ffmpeg-crf", "14",
    "--ffmpeg-output-parameters=-cq 14 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
    "--recording-system", "HLAE",
    "--close-game-after-recording",
]

# CSDM per-round sequence output (no --concatenate-sequences) and our renamed
# per-round file (round number encoded, unique + deterministic for resume).
_SEQ_RENDER_RE = re.compile(r"^sequence-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")
_ROUND_RENDER_RE = re.compile(r"^round-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")


def _copy_and_verify(src: Path, dst: Path) -> bool:
    """Copy src -> dst, then delete src only if dst exists and size matches."""
    if not src.exists():
        return False
    src_size = src.stat().st_size
    try:
        shutil.copy2(str(src), str(dst))
    except OSError:
        return False
    if not dst.exists():
        return False
    if dst.stat().st_size != src_size:
        try:
            dst.unlink()
        except OSError:
            pass
        return False
    try:
        if src.is_dir():
            shutil.rmtree(src, ignore_errors=True)
        else:
            src.unlink()
    except OSError:
        pass
    return True


def _rename_sequence_files(output_dir: Path, global_rounds: list[int]) -> set[int]:
    """Rename per-round CSDM outputs to round-{global:03d}-tick-{A}-to-{B}.mp4.

    Handles two CSDM output formats:
      Old (pre-3.20): sequence-{i}-tick-{A}-to-{B}.mp4 files in output_dir root.
      New (3.20+):    N-sequence/video.mp4 directories (tick info from analysis JSON).

    Returns the set of global round numbers successfully renamed. Best-effort
    salvage on partial success — when csdm emitted fewer outputs than requested
    (e.g. round 1 crashed but 2-7 rendered), surviving outputs are matched to
    rounds via tick spans in csdm_analysis.json (positional fallback if no
    analysis), and only unmapped rounds are left un-renamed. Won't sys.exit."""
    salvaged: set[int] = set()

    # Load tick map from analysis JSON
    analysis_path = output_dir / "csdm_analysis.json"
    tick_map: dict[int, tuple[int, int]] = {}
    if analysis_path.exists():
        try:
            data = json.loads(analysis_path.read_text(encoding="utf-8"))
            for r in data.get("rounds", []):
                rn = r.get("number")
                if rn is not None:
                    tick_map[rn] = (r.get("startTick", 0), r.get("endTick", 0))
        except Exception:
            pass

    # --- Old format: sequence-{i}-tick-{A}-to-{B}.mp4 ---
    seqs = sorted(
        (p for p in output_dir.glob("sequence-*-tick-*-to-*.mp4")),
        key=lambda p: int(_SEQ_RENDER_RE.match(p.name).group(1)),
    )
    if seqs:
        if len(seqs) == len(global_rounds):
            for i, p in enumerate(seqs):
                m = _SEQ_RENDER_RE.match(p.name)
                gr = global_rounds[i]
                dst = output_dir / f"round-{gr:03d}-tick-{m.group(2)}-to-{m.group(3)}.mp4"
                if _copy_and_verify(p, dst):
                    salvaged.add(gr)
            return salvaged

        # Partial match: fewer seq files than rounds. Map by tick start.
        tick_start_to_rn: dict[int, int] = {a: rn for rn, (a, _b) in tick_map.items()}
        for seq in seqs:
            m = _SEQ_RENDER_RE.match(seq.name)
            tick_s = int(m.group(2))
            rn = tick_start_to_rn.get(tick_s)
            if rn is not None and rn in set(global_rounds):
                dst = output_dir / f"round-{rn:03d}-tick-{m.group(2)}-to-{m.group(3)}.mp4"
                if _copy_and_verify(seq, dst):
                    salvaged.add(rn)
            else:
                # Fallback: sequential pairing with rescued rounds only
                pass
        # Fallback: any seqs we couldn't tick-map, pair sequentially with
        # remaining unmapped global_rounds
        unmapped_gr = [gr for gr in global_rounds if gr not in salvaged]
        unmapped_seqs = [
            s for s in seqs
            if s.parent == output_dir and s.name.startswith("sequence-")
        ]
        for seq, gr in zip(sorted(unmapped_seqs), unmapped_gr):
            m = _SEQ_RENDER_RE.match(seq.name)
            a, b = m.group(2), m.group(3)
            dst = output_dir / f"round-{gr:03d}-tick-{a}-to-{b}.mp4"
            if _copy_and_verify(seq, dst):
                salvaged.add(gr)
        return salvaged

    # --- New format (CSDM 3.20+): N-sequence/video.mp4 ---
    seq_dirs = sorted(
        (p for p in output_dir.glob("*-sequence") if p.is_dir()),
        key=lambda p: int(p.name.split("-")[0]),
    )
    if not seq_dirs:
        print(f"  [WARN] No sequence outputs found for batch {global_rounds}")
        return salvaged

    # CSDM numbers sequence dirs from 1 within each batch, starting at the first
    # round it was asked to render. seq_num → global_rounds[seq_num - 1].

    for seq_dir in seq_dirs:
        video = seq_dir / "video.mp4"
        if not video.exists():
            print(f"  [WARN] {video} not found in {seq_dir.name}")
            continue
        seq_num = int(seq_dir.name.split("-")[0])
        gr = global_rounds[seq_num - 1] if seq_num - 1 < len(global_rounds) else None
        if gr is None:
            continue
        a, b = tick_map.get(gr, (0, 0))
        dst = output_dir / f"round-{gr:03d}-tick-{a}-to-{b}.mp4"
        if _copy_and_verify(video, dst):
            salvaged.add(gr)

    return salvaged


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


def _is_pbdems2(demo_path) -> bool:
    """PBDEMS2 demos (FACEIT/PGL/BLAST) record per-player voice chat."""
    try:
        with open(demo_path, "rb") as f:
            return f.read(7) == b"PBDEMS2"
    except OSError:
        return False


def _write_render_autoexec(cvars: list[str]) -> None:
    # cl_chatfilters 48: hide server/system messages (16 Console + 32 Error),
    # keep player chat. Must match assets/cs2_pov.cfg — the cfg execs this
    # autoexec AFTER setting its own value, so this file wins.
    # Voice stays fully ON during render (CSDM --player-voices keeps
    # voice_enable on); no cl_mute_* cvars — cl_mute_enemy_team muted ALL
    # voice in PBDEMS2 demo playback and hid the talking indicators.
    lines = ["crosshair 1", "cl_chatfilters 48", "snd_mvp_volume 0",
             "snd_mute_losefocus 0"] + cvars
    content = "\n".join(lines) + "\n"
    AUTOEXEC_RENDER.write_text(content, encoding="utf-8")
    RENDER_CROSSHAIR_CFG.write_text(content, encoding="utf-8")


def _viewmodel_cvars_from_args(args: argparse.Namespace) -> list[str]:
    """Build viewmodel convars from CLI flags or prosettings nickname lookup."""
    from scrapers.prosettings import resolve_video_settings, viewmodel_convars

    settings: dict = {}
    if getattr(args, "viewmodel_fov", None) is not None:
        settings["viewmodel_fov"] = args.viewmodel_fov
    if getattr(args, "viewmodel_offset_x", None) is not None:
        settings["viewmodel_offset_x"] = args.viewmodel_offset_x
    if getattr(args, "viewmodel_offset_y", None) is not None:
        settings["viewmodel_offset_y"] = args.viewmodel_offset_y
    if getattr(args, "viewmodel_offset_z", None) is not None:
        settings["viewmodel_offset_z"] = args.viewmodel_offset_z
    if getattr(args, "viewmodel_presetpos", None) is not None:
        settings["viewmodel_presetpos"] = args.viewmodel_presetpos

    if not settings and getattr(args, "player", None):
        try:
            settings = resolve_video_settings(args.player, refresh_if_missing=True)
        except Exception as e:
            print(f"  [WARN] viewmodel lookup failed: {e}")
            settings = {}
    return viewmodel_convars(settings)


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


# Image names of every process the render pipeline spawns. Killing the whole
# tree (taskkill /t) is essential — csdm launches HLAE which launches ffmpeg,
# and a plain /im kill leaves the children (especially ffmpeg) running.
_RENDER_PROCESS_NAMES = ("cs2.exe", "HLAE.exe", "ffmpeg.exe", "csdm.exe", "csdm.cmd")


def _taskkill_tree(image_name: str) -> bool:
    """Force-kill a process image and its entire tree. Returns True if it ran."""
    try:
        r = subprocess.run(
            ["taskkill", "/f", "/t", "/im", image_name],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _process_running(image_name: str) -> bool:
    """True if any process with this image name is still alive."""
    try:
        r = subprocess.run(
            ["tasklist", "/fi", f"IMAGENAME eq {image_name}"],
            capture_output=True, text=True, timeout=15,
        )
        out = r.stdout or ""
        return image_name.lower() in out.lower() and "no tasks" not in out.lower()
    except Exception:
        return True  # assume alive on failure so we retry the kill


def _kill_stale_processes() -> None:
    """Kill every process left over from a previous render.

    Kills the whole process tree for each render binary (cs2, HLAE, ffmpeg, csdm),
    then polls tasklist and re-kills any survivors until they are gone (or a few
    retries elapse). ffmpeg and HLAE often linger after a crashed batch, so they
    are explicitly included and verified.
    """
    for name in _RENDER_PROCESS_NAMES:
        _taskkill_tree(name)

    # Give taskkill a beat, then hunt down anything that survived.
    for _ in range(6):
        survivors = [n for n in _RENDER_PROCESS_NAMES if _process_running(n)]
        if not survivors:
            break
        time.sleep(0.5)
        for n in survivors:
            _taskkill_tree(n)

    remaining = [n for n in _RENDER_PROCESS_NAMES if _process_running(n)]
    if remaining:
        print(f"  [WARN] could not kill: {', '.join(remaining)}")
    time.sleep(1)


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
    parser.add_argument("--width", type=int, default=None,
                        help="Render width (default: 2560, or player's capture_width from player_accounts.json).")
    parser.add_argument("--height", type=int, default=None,
                        help="Render height (default: 1440, or player's capture_height from player_accounts.json).")
    parser.add_argument("--batches", type=int, default=2,
                        help="Number of render batches (default: 2). Rounds are divided equally across batches. "
                             "E.g. --batches 3 with 30 rounds produces 3 batches of 10 rounds each.")
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
    parser.add_argument("--player", default="",
                        help="Player nickname for prosettings viewmodel lookup.")
    parser.add_argument("--viewmodel-fov", type=float, default=None)
    parser.add_argument("--viewmodel-offset-x", type=float, default=None)
    parser.add_argument("--viewmodel-offset-y", type=float, default=None)
    parser.add_argument("--viewmodel-offset-z", type=float, default=None)
    parser.add_argument("--viewmodel-presetpos", type=int, default=None)
    parser.add_argument("--skip-failed-rounds", action="store_true", default=False,
                        help="[DANGER] Skip round batches that fail instead of aborting the entire "
                             "render. Only use when a specific demo file is broken and you want to "
                             "render whatever rounds survive. NEVER enable by default. "
                             "Documented in AGENTS.md: this flag exists for corrupted/incompatible demos "
                             "where CS2 crashes on specific rounds. It silently drops failures, which "
                             "can produce incomplete POV videos. Only turn on per-invocation for "
                             "specifically problematic demos.")
    args = parser.parse_args()

    # Resolve capture resolution from player_accounts.json if available.
    # Fall back to 2560×1440 (16:9) when the player is not found or has no
    # capture dimensions set, or when the user explicitly passed --width/--height.
    _DEFAULT_W, _DEFAULT_H = 2560, 1440
    if args.width is None or args.height is None:
        _player_width, _player_height = None, None
        try:
            _accounts_path = _PROJECT_ROOT / ".data" / "player_accounts.json"
            if _accounts_path.exists():
                _accounts = json.loads(_accounts_path.read_text(encoding="utf-8"))
                _acct = next(
                    (a for a in _accounts if a.get("steam_id") == args.steam_id),
                    None,
                )
                if _acct:
                    _player_width = _acct.get("capture_width")
                    _player_height = _acct.get("capture_height")
        except Exception:
            pass

    if args.width is None:
        args.width = _player_width if _player_width and _player_width >= 800 else _DEFAULT_W
    if args.height is None:
        args.height = _player_height if _player_height and _player_height >= 600 else _DEFAULT_H

    _kill_stale_processes()
    _check_nvenc()

    demo_path = _ensure_demo(args.demo, args.hf_root, args.hf_repo, args.match_slug, args.match_id)

    from render_version_check import RenderVersionError, assert_render_versions

    try:
        vers = assert_render_versions(demo_path)
        print(
            f"  [OK] versions demo={vers.get('demo')} cs2={vers.get('cs2')} "
            f"hlae={vers.get('hlae')} csdm={vers.get('csdm')}"
        )
    except RenderVersionError as e:
        payload = json.dumps({
            "error": True,
            "step": 2,
            "step_name": "render",
            "code": e.code,
            "message": e.message,
        })
        print(f"[PIPELINE_ERROR] {payload}")
        sys.exit(1)
    parts = find_demo_parts(demo_path)
    print(f"Found {len(parts)} demo part(s):")
    for p in parts:
        print(f"  {Path(p).name}")

    output_dir = resolve_output_dir(args.output, parts[0], args.steam_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    cvars = _get_player_crosshair(args.steam_id, parts)
    vm_cvars = _viewmodel_cvars_from_args(args)
    if vm_cvars:
        print(f"  Viewmodel: {' | '.join(vm_cvars)}")
        cvars = list(cvars) + vm_cvars
    if cvars:
        print(f"  Player crosshair/viewmodel ({len(cvars)} cvars)")
        _write_render_autoexec(cvars)
        _swap_autoexec(AUTOEXEC_RENDER)
        print(f"  Swapped {AUTOEXEC_RENDER.name} -> {AUTOEXEC_MAIN.name}")
    else:
        print("  [WARN] No crosshair/viewmodel — keeping current autoexec.cfg")

    print(f"Output:  {output_dir}")
    total_rounds = sum(get_round_count(p) for p in parts)
    print(f"  {args.batches} batch(es) across {total_rounds} round(s)")
    print(f"  Resolution: {args.width}x{args.height} "
          f"(from player_accounts.json if available, else default)")

    if args.batches < 1:
        print("[ERROR] --batches must be >= 1")
        sys.exit(1)

    # Persistent skip list: rounds that failed with --skip-failed-rounds
    # so resume doesn't re-attempt them forever.
    SKIP_FILE = output_dir / ".skip_failed_rounds.json"
    skipped_rounds: set[int] = set()
    if args.skip_failed_rounds and SKIP_FILE.exists():
        try:
            skipped_rounds = set(json.loads(SKIP_FILE.read_text(encoding="utf-8")))
            if skipped_rounds:
                print(f"  [SKIP-FAILED] Loaded {len(skipped_rounds)} previously skipped round(s): "
                      f"{sorted(skipped_rounds)}")
        except Exception:
            pass

    try:
        minimizer = None
        if not args.no_minimize_cs2:
            # Park-behind: bring CS2 to the foreground for a beat on launch (so it
            # renders at full speed instead of being throttled while unactivated),
            # then drop it restored *behind* your other windows (not minimized).
            # fps_max 0 in the render cfg removes any FPS cap.
            minimizer = CS2Park()
            minimizer.start()
            print("CS2 park-behind enabled (briefly focuses on launch, then parks behind)")

        global_round = 0
        total_rendered = 0

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

            num_batches = args.batches
            n_desired = len(desired_local)
            if num_batches > n_desired:
                num_batches = n_desired

            # Divide rounds into num_batches roughly equal chunks.
            base, remainder = divmod(n_desired, num_batches)
            chunks: list[list[int]] = []
            offset = 0
            for b in range(num_batches):
                size = base + (1 if b < remainder else 0)
                chunks.append(desired_local[offset:offset + size])
                offset += size

            for batch in chunks:
                local_start = batch[0]
                local_end = batch[-1]
                global_start = global_round + local_start
                global_end = global_round + local_end

                # Per-round sequence files (no --concatenate-sequences): csdm
                # emits sequence-{i}-tick-{A}-to-{B}.mp4 per round. Rename to
                # round-{global:03d}-tick-A-to-B.mp4 so concat + resume can
                # address each round deterministically, and concat_rounds.py can
                # read the real per-round tick spans for per_round_ticks.
                global_rounds = [global_round + r for r in batch]
                already = {
                    int(_ROUND_RENDER_RE.match(p.name).group(1))
                    for p in output_dir.glob("round-*-tick-*-to-*.mp4")
                    if p.stat().st_size >= 1_048_576
                }
                # --skip-failed-rounds: treat previously failed rounds as "done"
                already |= skipped_rounds
                missing_global = [gr for gr in global_rounds if gr not in already]
                if not missing_global:
                    existing = [
                        p for p in output_dir.glob("round-*-tick-*-to-*.mp4")
                        if int(_ROUND_RENDER_RE.match(p.name).group(1)) in set(global_rounds)
                    ]
                    mb_total = sum(p.stat().st_size for p in existing) / 1024 / 1024
                    all_via_skip = set(global_rounds) <= skipped_rounds and not existing
                    if all_via_skip:
                        print(f"  [SKIP] rounds {global_rounds} previously failed (skip file)")
                    else:
                        print(f"  [SKIP] rounds {global_rounds} already rendered ({mb_total:.0f} MB)")
                        total_rendered += len(batch)
                    continue

                # Resume: only ask csdm for the missing local rounds in this batch.
                missing_local = [gr - global_round for gr in missing_global]
                cmd = [
                    CSDM, "video", str(Path(part).resolve()),
                    "--steamids", args.steam_id,
                    "--event", "rounds",
                    "--rounds", ",".join(str(r) for r in missing_local),
                    "--output", str(output_dir),
                    "--framerate", str(args.framerate),
                    "--width", str(args.width),
                    "--height", str(args.height),
                    "--cfg", str(abs_cfg_path()),
                ] + BASE_FLAGS

                # csdm writes per-round sequence files; we rename them below.
                failed_this_batch: list[int] = []
                csdm_crashed = False
                try:
                    run_csdm(cmd, f"rounds {missing_global[0]}-{missing_global[-1]}", expected=None)
                except SystemExit:
                    csdm_crashed = True
                    if not args.skip_failed_rounds:
                        raise

                # Always attempt salvage: even after a csdm crash, partial
                # sequence outputs may exist. Best-effort rename by tick span;
                # any round with no >=1 MB round-*.mp4 goes to the skip list.
                salvaged = _rename_sequence_files(output_dir, missing_global)
                if csdm_crashed:
                    print(f"  [WARN] csdm crashed (rounds {missing_global}); "
                          f"salvaged {len(salvaged)} rounds after crash")

                still = [
                    gr for gr in missing_global
                    if not any(
                        p.stat().st_size >= 1_048_576
                        for p in output_dir.glob(f"round-{gr:03d}-tick-*-to-*.mp4")
                    )
                ]
                if still:
                    if not args.skip_failed_rounds:
                        msg = f"[ERROR] After render, still missing rounds: {still}"
                        print(msg)
                        sys.exit(1)
                    failed_this_batch = still

                rendered_count = len(missing_global) - len(failed_this_batch)
                if rendered_count > 0:
                    total_rendered += rendered_count
                    print(f"  [OK] {rendered_count}/{len(missing_global)} rounds rendered "
                          f"for this batch")

                if args.skip_failed_rounds and failed_this_batch:
                    print(f"  [SKIP-FAILED] Dropped {len(failed_this_batch)} failed round(s): "
                          f"{failed_this_batch}")
                    skipped_rounds.update(failed_this_batch)
                    try:
                        SKIP_FILE.write_text(
                            json.dumps(sorted(skipped_rounds)),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass
                    # Kill stale CS2/HLAE processes after a failed batch to
                    # prevent cascade failures on subsequent batches.
                    _kill_stale_processes()

            global_round += n_rounds

        if minimizer:
            minimizer.stop()

        if total_rendered == 0:
            print("[ERROR] No rounds rendered")
            sys.exit(1)

        print(f"\nDone. {total_rendered} round(s) in {num_batches} batch(es) at {output_dir}")

        if args.overlay:
            # Run overlay on the rendered output
            vid = next((p for p in sorted(output_dir.glob("*.mp4"))
                        if p.stat().st_size >= 1_048_576), None)
            if vid is None or not vid.exists():
                print(f"[WARN] --overlay set but no video found: {vid}")
            else:
                round_arg = []
                if args.rounds:
                    # Use first round number for tick offset
                    r = args.rounds.split(",")[0].split("-")[0]
                    round_arg = ["--round", r]
                subprocess.run([
                    sys.executable, "scripts/overlay/overlay_pov.py",
                    "--video", str(vid),
                    "--demo", demo_path,
                    "--steam-id", args.steam_id,
                ] + round_arg, timeout=7200)
    finally:
        _swap_autoexec(AUTOEXEC_PERSONAL)
        print(f"  Swapped {AUTOEXEC_PERSONAL.name} -> {AUTOEXEC_MAIN.name} (restored)")


if __name__ == "__main__":
    main()
