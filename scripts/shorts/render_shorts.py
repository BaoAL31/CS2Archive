"""Render Short Timeline segments via CSDM and composite to 9:16 vertical.

Reads ``short_timeline.json``, renders tick-range source clips at the player's
native capture resolution (from player_accounts.json, e.g. 1280×960 for donk),
then composites each into 1080×1920 with edge mirror blur header/footer.

Usage:
    python scripts/shorts/render_shorts.py <short_timeline.json> [--player <steam_id>]
    python scripts/shorts/render_shorts.py <short_timeline.json> --batches 4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from shorts import resolve_output_dir  # noqa: E402
from shorts.make_short_meta import make_meta  # noqa: E402

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
CFG_PATH = (_PROJECT_ROOT / "assets" / "cs2_pov.cfg").resolve()

_DEFAULT_SRC_WIDTH = 1920
_DEFAULT_SRC_HEIGHT = 1080
# Backwards-compatible names used by older callers/tests.
SRC_WIDTH = _DEFAULT_SRC_WIDTH
SRC_HEIGHT = _DEFAULT_SRC_HEIGHT
OUT_WIDTH = 1080
OUT_HEIGHT = 1920
CSDM_RECORD_FRAMERATE = 60

KILLFEED_CROP_W = 300
KILLFEED_CROP_H = 170
KILLFEED_CROP_Y = 50

AVATAR_DIR = _PROJECT_ROOT / "demos" / "avatars"
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp")
AVATAR_SOURCES = ("hltv", "faceit")
AVATAR_DEFAULT_HEIGHT = 600
AVATAR_BOTTOM_MARGIN = 0
AVATAR_OUTLINE_WIDTH = 3


def _is_gpu_busy() -> bool:
    """Return True if GPU appears occupied by another ffmpeg/encode session.

    Checks nvidia-smi compute-apps first (most reliable — shows NVENC users),
    then falls back to tasklist/pgrep for any running ffmpeg process.
    Any hit = treat GPU as busy -> caller should switch to CPU (libx264).
    """
    # 1) nvidia-smi compute apps (NVENC shows as compute)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            lines = [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip() and "N/A" not in ln]
            # Only ffmpeg (or HLAE/CS2 encode) means NVENC busy — explorer/desktop
            # always shows in this list on Windows, ignore it
            if any("ffmpeg" in ln.lower() for ln in lines):
                return True
    except Exception:
        pass
    # 2) nvidia-smi broader gpu processes (covers newer drivers where NVENC not in compute-apps)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        # not decisive alone, so skip — rely on tasklist fallback instead
    except Exception:
        pass
    # 3) fallback: any ffmpeg process running at all (Windows tasklist / Unix pgrep)
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe"], capture_output=True, text=True, timeout=5)
        if "ffmpeg.exe" in r.stdout.lower():
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            return True
    except Exception:
        pass
    return False


def _resolve_use_cpu(force_cpu: bool, force_gpu: bool) -> bool:
    """Resolve encoder choice with auto-detection.

    --cpu  -> always CPU, --gpu -> always GPU, else auto: GPU busy -> CPU.
    """
    if force_cpu and force_gpu:
        print("[WARN] both --cpu and --gpu set — --cpu wins")
        return True
    if force_cpu:
        _dbg("enc", "forced CPU (libx264) via --cpu")
        return True
    if force_gpu:
        _dbg("enc", "forced GPU (h264_nvenc) via --gpu")
        return False
    busy = _is_gpu_busy()
    if busy:
        _dbg("enc", "auto: GPU busy (ffmpeg detected) -> switching to CPU (libx264)")
        return True
    _dbg("enc", "auto: GPU free -> using GPU (h264_nvenc)")
    return False


def _dbg(label: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] [{label}] {msg}", flush=True)


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return 0.0
    data = json.loads(r.stdout)
    return float(data.get("format", {}).get("duration", 0))


def _probe_resolution(path: Path) -> tuple[int, int]:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        return (0, 0)
    parts = r.stdout.strip().split(",")
    return (int(parts[0]), int(parts[1]))


def _ffmpeg_settings(use_cpu: bool = False) -> dict:
    if use_cpu:
        return {
            "constantRateFactor": 16,
            "videoContainer": "mp4",
            "videoCodec": "libx264",
            "audioCodec": "aac",
            "audioBitrate": 256,
            "inputParameters": "",
            "outputParameters": "-crf 16 -preset medium -profile:v high -pix_fmt yuv420p",
            "customLocationEnabled": True,
            "customExecutableLocation": FFMPEG,
        }
    return {
        "constantRateFactor": 14,
        "videoContainer": "mp4",
        "videoCodec": "h264_nvenc",
        "audioCodec": "aac",
        "audioBitrate": 256,
        "inputParameters": "",
        "outputParameters": "-cq 14 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
        "customLocationEnabled": True,
        "customExecutableLocation": FFMPEG,
    }


def _resolve_avatar_path(nickname: str) -> Path | None:
    """Return best transparent avatar cutout for a nickname.

    Mirrors thumbnail.utils.get_avatar_path: prefers the HLTV source folder,
    then FACEIT; within a folder picks the largest PNG by pixel area.
    """
    name = nickname.strip().lower()

    def _best_in(folder: Path) -> Path | None:
        if not folder.is_dir():
            return None
        cands: list[Path] = []
        for ext in AVATAR_EXTS:
            cands.extend(folder.glob(f"{name}*.{ext.lstrip('.')}"))
        if not cands:
            return None
        best = max(
            cands,
            key=lambda p: (Image.open(p).size[0] * Image.open(p).size[1])
            if _safe_size(p)
            else 0,
        )
        return best

    for source in AVATAR_SOURCES:
        p = _best_in(AVATAR_DIR / name / source)
        if p:
            return p
    return None


def _safe_size(p: Path) -> bool:
    try:
        Image.open(p).size
        return True
    except Exception:
        return False


def _prepare_avatar_overlay(
    avatar_path: Path,
    target_height: int,
    outline_width: int = AVATAR_OUTLINE_WIDTH,
    dst: Path | None = None,
) -> Path:
    """Scale a transparent avatar to *target_height* and bake a white outline.

    The cutout is resized (LANCZOS) and a solid-white rim ``outline_width`` px
    thick is grown around the alpha edge using a maximum filter. The result is
    saved to *dst* (or a sibling of *avatar_path*) as PNG and returned.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(avatar_path).convert("RGBA")
    w, h = img.size
    ratio = target_height / h
    img = img.resize((max(1, round(w * ratio)), target_height), Image.LANCZOS)

    if outline_width > 0:
        alpha = np.array(img.getchannel("A"), dtype=np.int32)
        kernel = outline_width * 2 + 1
        grown = np.array(img.getchannel("A").filter(ImageFilter.MaxFilter(kernel)), dtype=np.int32)
        # Ring = pixels the max-filter added (grown > original). Interior stays intact.
        ring = np.clip(grown - alpha, 0, 255).astype(np.uint8)
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        white.putalpha(Image.fromarray(ring))
        img = Image.alpha_composite(white, img)

    out = dst or avatar_path.with_name(f"{avatar_path.stem}_{target_height}px.png")
    img.save(out)
    return out


def _resolve_player_resolution(steam_id: str) -> tuple[int, int, str]:
    """Look up capture_width/capture_height + scaling_mode from player_accounts.json.
    
    Falls back to _DEFAULT_SRC_WIDTH × _DEFAULT_SRC_HEIGHT (1920×1080) + "Native"
    when the player is not found or has no capture dimensions.
    
    Returns: (width, height, scaling_mode) where scaling_mode is one of
    "Stretched", "Black Bars", "Native" (per prosettings/launch options).
    """
    accounts_path = _PROJECT_ROOT / ".data" / "player_accounts.json"
    if not accounts_path.exists():
        return (_DEFAULT_SRC_WIDTH, _DEFAULT_SRC_HEIGHT, "Native")
    try:
        accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
        match = next(
            (a for a in accounts if a.get("steam_id") == steam_id),
            None,
        )
        if match:
            w = match.get("capture_width")
            h = match.get("capture_height")
            scaling_mode = match.get("scaling_mode") or "Native"
            if w and w >= 800 and h and h >= 600:
                return (int(w), int(h), str(scaling_mode))
    except Exception:
        pass
    return (_DEFAULT_SRC_WIDTH, _DEFAULT_SRC_HEIGHT, "Native")


def _get_player_crosshair_cvars(steam_id: str, demo_path: Path) -> list[str]:
    cvars = []
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [CSDM, "json", str(demo_path.resolve()), "--output-folder", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return cvars
        jf = list(Path(tmp).glob("*.json"))
        if not jf:
            return cvars
        data = json.loads(jf[0].read_text(encoding="utf-8"))
        for pl in data.get("players", []):
            if pl.get("steamId") == steam_id:
                code = pl.get("crosshairShareCode")
                if code:
                    from crosshair_code import decode_crosshair, crosshair_to_convars
                    cvars = crosshair_to_convars(decode_crosshair(code))
                break
    return cvars


def _build_csdm_config(
    shorts: list[dict],
    demo_path: Path,
    output_dir: Path,
    src_width: int = _DEFAULT_SRC_WIDTH,
    src_height: int = _DEFAULT_SRC_HEIGHT,
    rename: bool = True,
    use_cpu: bool = False,
) -> dict:
    pov_sids = {s["pov_steam_id"] for s in shorts} | {s["pov_switch_to"] for s in shorts if "pov_switch_to" in s}
    crosshair_cache = {}
    for sid in pov_sids:
        cvars = _get_player_crosshair_cvars(sid, demo_path)
        if cvars:
            crosshair_cache[sid] = cvars
            _dbg("xhair", f"crosshair for {sid}: {len(cvars)} cvars")

    sequences = []
    seq_num = 1

    for s in shorts:
        start_tick = s["start_tick"]
        end_tick = s["end_tick"]
        if start_tick >= end_tick:
            start_tick, end_tick = end_tick, start_tick
        if end_tick - start_tick < 64:
            end_tick = start_tick + 64

        pov_sid = s["pov_steam_id"]
        cvars = crosshair_cache.get(pov_sid, [])

        cfg_lines = [
            "cl_draw_only_deathnotices 1",
            "crosshair 1",
            "cl_chatfilters 63",
            "snd_mvp_volume 0",
            "cl_showfps 0",
            "net_graph 0",
        ] + cvars

        # Automatically rename the POV player's in-HUD name to their canonical
        # nickname (HLAE mirv_replace_name, 2.184+). The demo records the
        # FACEIT account name (e.g. "CEMEN_BAKIN"); pov_nick is the canonical
        # nickname from player_accounts.json (e.g. "kyousuke"). byXuid is stable
        # per-player across demos. Only covers "some parts of the HUD" — chat is
        # not replaced.
        if rename:
            nick = (s.get("pov_nick") or "").strip()
            if nick and nick.lower() != "unknown":
                cfg_lines.append(f'mirv_replace_name byXuid add x{pov_sid} "{nick}"')

        cfg_text = "\n".join(cfg_lines) + "\n"

        seq = {
            "number": seq_num,
            "startTick": start_tick,
            "endTick": end_tick,
            "cfg": cfg_text,
            "showXRay": False,
            "showAssists": True,
            "showOnlyDeathNotices": False,
            "playersOptions": [],
            "cameras": [],
            "playerCameras": [
                {"tick": start_tick, "playerSteamId": pov_sid, "playerName": "pov"},
            ] + ([{"tick": s["pov_switch_tick"], "playerSteamId": s["pov_switch_to"], "playerName": "pov_switch"}] if "pov_switch_tick" in s and "pov_switch_to" in s else []),
            "playerVoicesEnabled": False,
            "recordAudio": True,
            "deathNoticesDuration": 5,
        }
        sequences.append(seq)
        seq_num += 1

    return {
        "demoPath": str(demo_path.resolve()),
        "outputFolderPath": str(output_dir.resolve()),
        "recordingSystem": "HLAE",
        "recordingOutput": "video",
        "encoderSoftware": "FFmpeg",
        "framerate": CSDM_RECORD_FRAMERATE,
        "width": src_width,
        "height": src_height,
        "closeGameAfterRecording": True,
        "concatenateSequences": False,
        "trueView": False,
        "ffmpegSettings": _ffmpeg_settings(use_cpu=use_cpu),
        "sequences": sequences,
    }


def _run_csdm(config_path: Path) -> int:
    cmd = [CSDM, "video", "--config-file", str(config_path.resolve())]
    _dbg("csdm", f"command: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        print(out[-3000:] if proc.returncode == 0 else out, flush=True)
    _dbg("csdm", f"exit code: {proc.returncode}")
    return proc.returncode


def _run_csdm_hook_aware(config_path: Path, segments_dir: Path, label: str,
                          hook_timeout: float, hook_retries: int) -> None:
    """Render a CSDM short-batch with HLAE hook-failure detection + retry.

    Mirrors scripts/pov/render_pov.py's hook-aware path: poll ``segments_dir``
    for a newly-produced sequence file (the signal that HLAE actually hooked
    CS2 and is encoding). If none appears within ``hook_timeout`` seconds, kill
    the CS2/HLAE tree and retry, up to ``hook_retries`` times. Exits if the
    hook never engages.
    """
    from render_pov import _kill_stale_processes

    cmd = [CSDM, "video", "--config-file", str(config_path.resolve())]
    for attempt in range(1, hook_retries + 1):
        suffix = f" (attempt {attempt}/{hook_retries})" if hook_retries > 1 else ""
        print(f"  [{label}]{suffix}...", end=" ", flush=True)
        before_n = len(_find_sequence_files(segments_dir, 9999))
        t0 = time.time()
        log_path = segments_dir.parent / f".csdm_hook_attempt_{attempt}.log"
        with open(log_path, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True)
            engaged = False
            poll_start = time.time()
            while time.time() - poll_start < hook_timeout:
                if proc.poll() is not None:
                    break
                if len(_find_sequence_files(segments_dir, 9999)) > before_n:
                    engaged = True
                    break
                time.sleep(5)
            if not engaged:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
                _kill_stale_processes()
                print(f"HOOK-FAIL (no sequence in {hook_timeout:.0f}s) - killing and retrying")
                continue
            proc.wait(timeout=14400)
        print(f"OK ({time.time() - t0:.0f}s)")
        return

    print(f"[ERROR] CS2 failed to hook after {hook_retries} attempt(s) "
          f"(no sequence produced in {hook_timeout:.0f}s).")
    sys.exit(1)


def _find_sequence_files(search_dir: Path, num_expected: int, shorts: list[dict] | None = None) -> list[Path]:
    import re
    seq_dirs = sorted(
        [d for d in search_dir.iterdir()
         if d.is_dir() and re.match(r"^\d+-sequence$", d.name)],
        key=lambda d: int(d.name.split("-")[0]),
    )
    if seq_dirs:
        files = [d / "video.mp4" for d in seq_dirs if (d / "video.mp4").is_file()]
        _dbg("find", f"found {len(files)} sequence dirs")
        return files

    flat = [p for p in search_dir.glob("sequence-*-tick-*-to-*.mp4")
            if not p.name.endswith("-kill_feed.mp4")]
    if flat and shorts:
        by_ticks: dict[tuple[int, int], Path] = {}
        for p in flat:
            m = re.match(r"sequence-\d+-tick-(\d+)-to-(\d+)", p.name)
            if m:
                by_ticks[(int(m.group(1)), int(m.group(2)))] = p
        matched = []
        for s in shorts:
            key = (int(s["start_tick"]), int(s["end_tick"]))
            if key in by_ticks:
                matched.append(by_ticks[key])
        if len(matched) == len(shorts):
            return matched

    if flat:
        flat = sorted(flat, key=lambda p: p.stat().st_mtime)[-num_expected:]
        flat = sorted(flat, key=lambda p: int(re.match(r"sequence-(\d+)", p.name).group(1)))
        return flat

    fallback = sorted(search_dir.rglob("video.mp4"))
    if fallback:
        return fallback
    return []


def _render_kill_feed_pip(src: Path, dst: Path, src_width: int = _DEFAULT_SRC_WIDTH, use_cpu: bool = False) -> None:
    """Pre-render the kill-feed PiP from the source segment.

    Crops the top band of the source (KILLFEED_CROP_W x KILLFEED_CROP_H at
    y=0) at native resolution — no rescaling, so no resampling artifacts.
    Encode is GPU (h264_nvenc) or CPU (libx264) depending on *use_cpu*.
    Single ffmpeg call; the result is fed into ``_composite_9x16`` as input
    [1:v] so the final composite remains a single encode pass.
    """
    kf_x = src_width - KILLFEED_CROP_W
    vf = f"crop={KILLFEED_CROP_W}:{KILLFEED_CROP_H}:{kf_x}:{KILLFEED_CROP_Y}"
    if use_cpu:
        vcodec = ["-c:v", "libx264", "-crf", "16", "-preset", "medium"]
    else:
        vcodec = ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14"]
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", vf,
        *vcodec,
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(dst),
    ]

    _dbg("kf", f"{src.name}: crop {KILLFEED_CROP_W}x{KILLFEED_CROP_H}@{kf_x},{KILLFEED_CROP_Y} (native, no scale)")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(
            f"kill feed pre-render failed (rc={r.returncode}): {r.stderr[-2000:]}"
        )
    mb = dst.stat().st_size / 1e6
    _dbg("kf", f"OK ({mb:.2f} MB) -> {dst.name}")


def _composite_9x16(
    src: Path,
    dst: Path,
    src_width: int = _DEFAULT_SRC_WIDTH,
    src_height: int = _DEFAULT_SRC_HEIGHT,
    scale: float = 2.0,
    blur_radius: int = 50,
    darken_factor: float = 1.0,
    kill_feed: bool = True,
    kill_feed_path: Path | None = None,
    pip_scale: float = 1.0,
    scaling_mode: str = "Native",
    avatar_path: Path | None = None,
    avatar_height: int = AVATAR_DEFAULT_HEIGHT,
    avatar_bottom_margin: int = AVATAR_BOTTOM_MARGIN,
    use_cpu: bool = False,
) -> None:
    """Composite a source clip into 1080x1920 via ffmpeg filter chain.

    Background: source scaled to fill canvas (force_original_aspect_ratio=increase,
    crop to 1080x1920), Gaussian-blurred.

    Foreground: source scaled to fit canvas (force_original_aspect_ratio=decrease).
    ``scale=1.0`` fits the source inside without cropping.
    ``scale>1.0`` zooms in to a consistent footprint regardless of the source
    aspect ratio: the foreground is scaled to the same height a 16:9 source
    would reach at that zoom (``OUT_WIDTH * scale * 9/16``), then the width is
    centre-cropped to 1080. 4:3 black-bars players no longer blow up to fill
    the canvas height (previously 1080×1620 vs 1080×1215 for 16:9).

    4:3 stretched players (e.g. donk, s1mple): the source is captured at the
    4:3 capture resolution but the player's view is *horizontally stretched*
    to fill 16:9 (scaling_mode=Stretched). Pre-scale the source horizontally
    to 16:9 before compositing — ``setsar=1`` ensures the DAR is square so
    ffmpeg doesn't pillarbox/unsquish downstream.

    Kill-feed PiP (when ``kill_feed=True``, default): direct native crop from
    the source (no per‑PiP scaling) overlaid at top‑right of the foreground
    footage. Carved from [0:v] so it sits on the same source timeline as
    bg/fg — downstream overlays are on the final 1080×1920 canvas.

    Player avatar (when ``avatar_path`` is given): the transparent cutout is
    overlaid at the bottom-centre of the 1080×1920 canvas, scaled to a target
    height (``avatar_height``) with ``avatar_bottom_margin`` px of clearance
    from the bottom edge.

    Encode: NVIDIA NVENC (h264_nvenc) for GPU-accelerated H.264, or
    libx264 for CPU when *use_cpu* is True (allows parallel ffmpeg
    sessions when GPU already occupied).
    Decode: cuda hwaccel for GPU path; CPU path uses no hwaccel.
    Audio: passthrough copy from the source.
    """
    gblur_sigma = max(1, blur_radius // 2)

    # Encoder / hwaccel selection (shared by both kill_feed paths)
    if use_cpu:
        vcodec_composite = ["-c:v", "libx264", "-crf", "16", "-preset", "medium", "-profile:v", "high", "-pix_fmt", "yuv420p"]
        hwaccel_prefix: list[str] = []
    else:
        vcodec_composite = ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14", "-b:v", "0", "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "4.2"]
        hwaccel_prefix = ["-hwaccel", "cuda"]

    # 4:3 stretched players: pre-stretch source to 16:9 horizontally so
    # bg/fg/pip all see a 16:9 source. Stretch target height = round(W_src * 9/16)
    # only when source is 4:3 (or 5:4) AND scaling_mode=Stretched.
    src_aspect = src_width / src_height if src_height else 16 / 9
    is_4_3_like = 1.20 <= src_aspect <= 1.40
    stretch = (scaling_mode or "Native").lower() == "stretched" and is_4_3_like
    if stretch:
        stretched_w = src_width
        stretched_h = round(src_width * 9 / 16)
        _dbg("9x16", f"{src.name}: 1080x1920 canvas, src={src_width}x{src_height} ({scaling_mode} -> stretch to {stretched_w}x{stretched_h}), scale={scale}x, blur_sigma={gblur_sigma}")
    else:
        stretched_w, stretched_h = src_width, src_height
        _dbg("9x16", f"{src.name}: 1080x1920 canvas, src={src_width}x{src_height} ({scaling_mode}), scale={scale}x, blur_sigma={gblur_sigma}")

    # Optional stretch pre-filter applied to [0:v] before split into bg/fg/pip_src.
    # Two-stage: scale+stretch first (output=[base]), then split from [base].
    if stretch:
        pre_stretch = f"[0:v]scale={stretched_w}:{stretched_h}:flags=spline,setsar=1[base];"
        split_pre = "[base]split="
    else:
        pre_stretch = ""
        split_pre = "[0:v]split="

    if scale == 1.0:
        fg_chain = (
            f"[fg_src]scale={OUT_WIDTH}:{OUT_HEIGHT}:"
            f"force_original_aspect_ratio=decrease[fg]"
        )
        fg_top = 0
    else:
        fg_h = round(OUT_WIDTH * scale * 9 / 16)
        fg_chain = (
            f"[fg_src]scale=-1:{fg_h},"
            f"crop={OUT_WIDTH}:{fg_h}[fg]"
        )
        fg_top = (OUT_HEIGHT - fg_h) // 2

    bg_chain = (
        f"[bg_src]scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={OUT_WIDTH}:{OUT_HEIGHT},"
        f"gblur=sigma={gblur_sigma}:steps=2[bg]"
    )
    if darken_factor < 1.0:
        bg_chain += f",eq=brightness={round(darken_factor - 1.0, 3)}"

    if kill_feed:
        pip_w = KILLFEED_CROP_W
        pip_h = KILLFEED_CROP_H
        
        actual_pip_scale = pip_scale
        if actual_pip_scale <= 0.0 and scale != 1.0 and fg_top > 0:
            # Default: fill the full canvas width, capped at the header band height.
            width_limit = OUT_WIDTH / pip_w
            height_limit = fg_top / pip_h
            actual_pip_scale = min(width_limit, height_limit)
        elif actual_pip_scale <= 0.0:
            actual_pip_scale = 1.0
            
        scaled_pip_w = int(pip_w * actual_pip_scale)
        scaled_pip_h = int(pip_h * actual_pip_scale)
        pip_x = OUT_WIDTH - scaled_pip_w
        
        # Place it vertically centered inside the top blurred banner
        if scale != 1.0 and fg_top > scaled_pip_h:
            pip_y = (fg_top - scaled_pip_h) // 2
        else:
            pip_y = 0

        cmd = [
            FFMPEG, "-y", *hwaccel_prefix,
            "-i", str(src)
        ]

        if kill_feed_path:
            cmd.extend(["-i", str(kill_feed_path)])
            pip_scale_chain = f"[1:v]scale={scaled_pip_w}:{scaled_pip_h}[scaled_pip];" if actual_pip_scale != 1.0 else ""
            pip_in = "[scaled_pip]" if actual_pip_scale != 1.0 else "[1:v]"
            if stretch:
                split_part = f"{pre_stretch}{split_pre}2[bg_src][fg_src];"
            else:
                split_part = "[0:v]split=2[bg_src][fg_src];"
            filter_str = (
                f"{split_part}"
                f"{bg_chain};"
                f"{fg_chain};"
                f"{pip_scale_chain}"
                f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[tmp];"
                f"[tmp]{pip_in}overlay={pip_x}:{pip_y}[out]"
            )
        else:
            pip_chain = (
                f"[pip_src]crop={pip_w}:{pip_h}:"
                f"{stretched_w - pip_w}:{KILLFEED_CROP_Y}"
            )
            if actual_pip_scale != 1.0:
                pip_chain += f",scale={scaled_pip_w}:{scaled_pip_h}"
            pip_chain += "[pip];"
            
            if stretch:
                split_part = f"{pre_stretch}{split_pre}3[bg_src][fg_src][pip_src];"
            else:
                split_part = "[0:v]split=3[bg_src][fg_src][pip_src];"
            filter_str = (
                f"{split_part}"
                f"{bg_chain};"
                f"{fg_chain};"
                f"{pip_chain}"
                f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[tmp];"
                f"[tmp][pip]overlay={pip_x}:{pip_y}[out]"
            )

        if avatar_path:
            av_idx = 2 if kill_feed_path else 1
            cmd.extend(["-i", str(avatar_path)])
            filter_str += (
                f";[{av_idx}:v]scale=-1:{avatar_height}[av];"
                f"[out][av]overlay=(main_w-overlay_w)/2:"
                f"(main_h-overlay_h-{avatar_bottom_margin})[out]"
            )

        cmd.extend([
            "-filter_complex", filter_str,
            "-map", "[out]", "-map", "0:a?",
            *vcodec_composite,
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ])
    else:
        if stretch:
            split_part = f"{pre_stretch}{split_pre}2[bg_src][fg_src];"
        else:
            split_part = "[0:v]split=2[bg_src][fg_src];"
        vf = (
            f"{split_part}"
            f"{bg_chain};"
            f"{fg_chain};"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[out]"
        )
        cmd = [
            FFMPEG, "-y", *hwaccel_prefix, "-i", str(src),
            "-filter_complex", vf,
            "-map", "[out]", "-map", "0:a?",
            *vcodec_composite,
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ]
        if avatar_path:
            fc_idx = cmd.index("-filter_complex")
            cmd[fc_idx:fc_idx] = ["-i", str(avatar_path)]
            vf += (
                f";[1:v]scale=-1:{avatar_height}[av];"
                f"[out][av]overlay=(main_w-overlay_w)/2:"
                f"(main_h-overlay_h-{avatar_bottom_margin})[out]"
            )
            cmd[cmd.index("-filter_complex") + 1] = vf

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg composite failed (rc={r.returncode}): {r.stderr[-2000:]}"
        )

    mb = dst.stat().st_size / 1e6
    _dbg("composite", f"OK ({mb:.1f} MB)")


def _short_output_path(out_dir: Path, short: dict, name: str | None = None) -> Path:
    """Compute the final 9:16 output path for a short (or ``name`` override)."""
    if name:
        base = name
    else:
        st = short["short_type"]
        nick = short.get("pov_nick", "unknown")
        tick = short.get("start_tick", 0)
        if st == "4k":
            kills = len(short.get("kill_ticks", []))
            base = f"{kills}k_multikill-{nick}-t{tick}"
        elif st == "clutch":
            cnt = short.get("clutch_initial_count", "XvX")
            base = f"{cnt}_clutch-{nick}-t{tick}"
        else:
            base = f"{st}-{nick}-t{tick}"
        tags = short.get("punch_up_tags") or []
        if tags:
            base = f"{base}_{'_'.join(tags)}"
    return out_dir / f"{base}.mp4"


def render_shorts(
    timeline_path: Path,
    player: str | None = None,
    batch_size: int = 0,
    scale: float = 2.0,
    pip_scale: float = 1.0,
    composite_only: bool = False,
    name: str | None = None,
    avatar: bool = True,
    avatar_height: int = AVATAR_DEFAULT_HEIGHT,
    avatar_bottom_margin: int = AVATAR_BOTTOM_MARGIN,
    avatar_outline_width: int = AVATAR_OUTLINE_WIDTH,
    rename: bool = True,
    make_meta_on: bool = True,
    tournament: str | None = None,
    year: str = "2026",
    hook_timeout: float = 150.0,
    hook_retries: int = 2,
    use_cpu: bool = False,
) -> Path:
    """Render all shorts from a timeline JSON.
    
    When *composite_only* is True, skip CSDM and re-composite existing
    segments with new editing parameters (e.g. scale).
    
    When *avatar* is True, each short's player avatar (transparent cutout,
    resolved from ``pov_nick``) is overlaid at the bottom-centre of the
    9:16 canvas.
    
    Returns the output directory path.
    """
    tl = json.loads(timeline_path.read_text(encoding="utf-8"))
    shorts = tl.get("shorts", [])
    if not shorts:
        raise ValueError("No shorts in timeline")

    demo_path = Path(tl["demo_path"])
    if not demo_path.exists():
        raise FileNotFoundError(f"Demo not found: {demo_path}")

    # Resolve player capture resolution from player_accounts.json.
    # Use the first short's POV to determine render resolution.
    pov_sid = shorts[0].get("pov_steam_id", "")
    src_w, src_h, scaling_mode = _resolve_player_resolution(pov_sid)
    _dbg("res", f"Player resolution: {src_w}x{src_h} ({scaling_mode}, pov_steam_id={pov_sid})")

    # If the timeline is in a per-short folder (shorts-{slug}/), output there.
    # Otherwise fall back to resolve_output_dir base.
    parent = timeline_path.resolve().parent
    if parent.name.startswith("shorts-"):
        out_dir = parent
    else:
        out_dir = resolve_output_dir(demo_path, player=player)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments_dir = out_dir / "segments"

    print(f"Shorts: {len(shorts)} clips, map={tl.get('map', 'Unknown')}")
    print(f"Demo: {demo_path}")
    print(f"Output: {out_dir}")
    print(f"Render resolution: {src_w}x{src_h}")

    # Split into batches
    if batch_size > 0:
        batches = [shorts[i:i + batch_size] for i in range(0, len(shorts), batch_size)]
    else:
        batches = [shorts]

    print(f"  Batches: {len(batches)} (batch size: {batch_size if batch_size > 0 else 'all'})")

    # Early resume: if every short's final output already exists at the target
    # resolution, skip the whole pipeline (CSDM render + composite). The per-short
    # composite skip below alone is pointless — it fires AFTER the expensive
    # CSDM segment render.
    if not composite_only:
        all_done = True
        for short in shorts:
            dst = _short_output_path(out_dir, short, name)
            if not (dst.exists() and dst.stat().st_size >= 1_048_576):
                all_done = False
                break
            w, h = _probe_resolution(dst)
            if not (w == OUT_WIDTH and h == OUT_HEIGHT):
                all_done = False
                break
        if all_done:
            print(f"  [SKIP] all {len(shorts)} short(s) already rendered at "
                  f"{OUT_WIDTH}x{OUT_HEIGHT} — nothing to do")
            return out_dir

    if not composite_only:
        # Fresh render: clear stale segments, then run CSDM for each batch
        if segments_dir.exists():
            import shutil
            shutil.rmtree(segments_dir)
        segments_dir.mkdir(parents=True, exist_ok=True)

        for batch_idx, batch in enumerate(batches):
            batch_start = sum(len(b) for b in batches[:batch_idx])
            batch_end = batch_start + len(batch) - 1
            _dbg("batch", f"batch {batch_idx + 1}/{len(batches)}: shorts {batch_start + 1}-{batch_end + 1}")

            config = _build_csdm_config(batch, demo_path, segments_dir, src_w, src_h, rename, use_cpu=use_cpu)
            conf_path = out_dir / f"batch_{batch_idx + 1}_config.json"
            conf_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

            if hook_timeout > 0 and hook_retries > 0:
                _run_csdm_hook_aware(
                    conf_path, segments_dir,
                    f"batch {batch_idx + 1}/{len(batches)}",
                    hook_timeout, hook_retries,
                )
            else:
                ret = _run_csdm(conf_path)
                if ret != 0:
                    raise RuntimeError(
                        f"CSDM batch {batch_idx + 1} failed (exit {ret})")
            _dbg("csdm", f"batch {batch_idx + 1} rendered")

            seq_files = _find_sequence_files(segments_dir, len(batch), batch)
            if len(seq_files) != len(batch):
                _dbg("render", f"[WARN] Expected {len(batch)} sequence files, found {len(seq_files)}")
    else:
        if not segments_dir.exists():
            raise FileNotFoundError(f"No existing segments dir for --composite-only: {segments_dir}")
        _dbg("composite", f"composite-only: reusing {len(shorts)} source clips from {segments_dir}")

    # Composite each segment into 9:16
    seq_files = _find_sequence_files(segments_dir, len(shorts), shorts)
    if not seq_files:
        seq_files = sorted(segments_dir.rglob("video.mp4"))
        if not seq_files:
            raise RuntimeError(f"No rendered sequence files found in {segments_dir}")

    for i, (seg_file, short) in enumerate(zip(seq_files, shorts)):
        dst = _short_output_path(out_dir, short, name)
        out_name = dst.name
        if dst.exists() and dst.stat().st_size >= 1_048_576:
            w, h = _probe_resolution(dst)
            if w == OUT_WIDTH and h == OUT_HEIGHT:
                _dbg("composite", f"[SKIP] {out_name} already rendered at {OUT_WIDTH}x{OUT_HEIGHT}")
                continue
        kf_path = segments_dir / f"{seg_file.stem}-kill_feed.mp4"
        if not kf_path.exists() or kf_path.stat().st_size < 1_048_576:
            _render_kill_feed_pip(seg_file, kf_path, src_w, use_cpu=use_cpu)

        avatar_path = None
        if avatar:
            nick = short.get("pov_nick", "")
            avatar_path = _resolve_avatar_path(nick) if nick else None
            if avatar_path:
                _dbg("avatar", f"{out_name}: avatar {avatar_path.name}")
                overlay_path = out_dir / f"_avatar_{nick}.png"
                avatar_path = _prepare_avatar_overlay(
                    avatar_path, avatar_height,
                    outline_width=avatar_outline_width, dst=overlay_path,
                )
            else:
                _dbg("avatar", f"{out_name}: no avatar for '{nick}' — skipping overlay")

        _composite_9x16(
            seg_file, dst, src_w, src_h,
            scale=scale, kill_feed_path=kf_path, pip_scale=pip_scale,
            scaling_mode=scaling_mode, avatar_path=avatar_path,
            avatar_height=avatar_height, avatar_bottom_margin=avatar_bottom_margin,
            use_cpu=use_cpu,
        )
        w, h = _probe_resolution(dst)
        dur = _probe_duration(dst)
        _dbg("done", f"{out_name}: {w}x{h} {dur:.1f}s (pov: {short['pov_steam_id']}, type: {short['short_type']})")

    # Generate YouTube-Shorts upload meta. Team/org is detected from the demo
    # itself (scripts.shorts.detect_team) — never from memory.
    if make_meta_on:
        try:
            meta = make_meta(out_dir, tournament=tournament, year=year)
            _dbg("meta", f"wrote upload_meta_shorts.json: {meta['title']}")
        except Exception as e:
            _dbg("meta", f"[WARN] meta generation failed: {e}")

    print(f"\nDone. Shorts in {out_dir}")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Render Short Timeline segments + composite to 9:16")
    ap.add_argument("timeline", type=Path, help="Path to short_timeline.json")
    ap.add_argument("--player", type=str, default=None, help="Steam ID for HLTV demo output dir")
    ap.add_argument("--output", "-o", type=Path, default=None, help="Override output directory")
    ap.add_argument("--batches", type=int, default=0,
                   help="Shorts per batch (0 = all in one)")
    ap.add_argument("--scale", type=float, default=2.0,
                   help="Foreground scale multiplier (1.0 = fit canvas, 2.0 = 2x zoom with centre crop)")
    ap.add_argument("--pip-scale", type=float, default=0.0,
                   help="Kill feed scale multiplier (0.0 = auto-fill top banner height)")
    ap.add_argument("--composite-only", action="store_true",
                   help="Skip CSDM render; re-composite existing segments with new editing params")
    ap.add_argument("--name", type=str, default=None,
                   help="Output filename (without .mp4). Default: {short_type}-{pov_nick}")
    ap.add_argument("--no-avatar", action="store_true",
                   help="Skip the player avatar overlay (bottom-centre)")
    ap.add_argument("--avatar-height", type=int, default=AVATAR_DEFAULT_HEIGHT,
                   help=f"Avatar target height in px on the 1080x1920 canvas (default: {AVATAR_DEFAULT_HEIGHT})")
    ap.add_argument("--avatar-bottom-margin", type=int, default=AVATAR_BOTTOM_MARGIN,
                   help=f"Clearance from the bottom edge in px (default: {AVATAR_BOTTOM_MARGIN})")
    ap.add_argument("--avatar-outline-width", type=int, default=AVATAR_OUTLINE_WIDTH,
                   help=f"White outline width around the avatar in px (default: {AVATAR_OUTLINE_WIDTH}; 0 = none)")
    ap.add_argument("--no-meta", action="store_true",
                   help="Skip auto-generating upload_meta_shorts.json after rendering")
    ap.add_argument("--tournament", type=str, default=None,
                   help="Override tournament hashtag (e.g. 'esportworldcup2026'); "
                        "auto-detected from the demo folder otherwise")
    ap.add_argument("--year", type=str, default="2026",
                   help="Year suffix for the auto-detected tournament hashtag (default: 2026)")
    ap.add_argument("--no-rename", action="store_true",
                   help="Disable automatic HUD player-name rename to canonical nickname "
                        "(mirv_replace_name). Default: on.")
    ap.add_argument("--hook-timeout", type=float, default=150.0,
                   help="Seconds to wait for HLAE to hook CS2 and emit a sequence file "
                        "before treating it as a hook failure (default: 150). "
                        "0 disables hook detection.")
    ap.add_argument("--hook-retries", type=int, default=2,
                   help="Retries after a failed HLAE hook before giving up "
                        "(default: 2). 0 disables hook detection.")
    ap.add_argument("--cpu", action="store_true",
                   help="Force CPU encoder (libx264) instead of GPU (h264_nvenc + cuda).")
    ap.add_argument("--gpu", action="store_true",
                   help="Force GPU encoder (h264_nvenc + cuda) even if another ffmpeg is running.")
    ap.add_argument("--no-auto", action="store_true",
                   help="Disable auto GPU-busy detection (default: auto-detect; GPU busy -> CPU).")
    args = ap.parse_args()

    try:
        if args.no_auto:
            use_cpu = args.cpu and not args.gpu
            if not args.cpu and not args.gpu:
                _dbg("enc", "auto disabled (--no-auto) -> default GPU")
                use_cpu = False
            elif args.cpu:
                _dbg("enc", "forced CPU via --cpu (--no-auto)")
                use_cpu = True
            else:
                _dbg("enc", "forced GPU via --gpu (--no-auto)")
                use_cpu = False
        else:
            use_cpu = _resolve_use_cpu(args.cpu, args.gpu)
        render_shorts(args.timeline, player=args.player, batch_size=args.batches,
                       scale=args.scale, pip_scale=args.pip_scale, composite_only=args.composite_only,
                       name=args.name, avatar=not args.no_avatar,
                       avatar_height=args.avatar_height,
                       avatar_bottom_margin=args.avatar_bottom_margin,
                       avatar_outline_width=args.avatar_outline_width,
                       rename=not args.no_rename, make_meta_on=not args.no_meta,
                       tournament=args.tournament, year=args.year,
                       hook_timeout=args.hook_timeout, hook_retries=args.hook_retries,
                       use_cpu=use_cpu)
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())