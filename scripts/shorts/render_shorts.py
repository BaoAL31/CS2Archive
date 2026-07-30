"""Render Short Timeline segments via CSDM and composite to 9:16 vertical.

Reads ``short_timeline.json``, renders tick-range source clips (2560×1440),
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

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from shorts import resolve_output_dir  # noqa: E402

CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"
FFMPEG = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE = r"C:\Users\jembo\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
CFG_PATH = (_PROJECT_ROOT / "assets" / "cs2_pov.cfg").resolve()

SRC_WIDTH = 2560
SRC_HEIGHT = 1440
OUT_WIDTH = 1080
OUT_HEIGHT = 1920
CSDM_RECORD_FRAMERATE = 64


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


def _ffmpeg_settings() -> dict:
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
) -> dict:
    pov_sids = {s["pov_steam_id"] for s in shorts}
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
            "crosshair 1",
            "cl_chatfilters 63",
            "snd_mvp_volume 0",
            "cl_draw_only_deathnotices 0",
            "cl_drawhud 1",
            "cl_showfps 0",
            "net_graph 0",
        ] + cvars
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
            ],
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
        "width": SRC_WIDTH,
        "height": SRC_HEIGHT,
        "closeGameAfterRecording": True,
        "concatenateSequences": False,
        "trueView": False,
        "ffmpegSettings": _ffmpeg_settings(),
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

    flat = list(search_dir.glob("sequence-*-tick-*-to-*.mp4"))
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


def _composite_9x16(
    src: Path,
    dst: Path,
    footage_ratio: int = 10,
    blur_sigma: int = 40,
    bg_scale: float = 1.5,
    side_crop: float = 0.10,
) -> None:
    """Composite a 2560x1440 source into 1080x1920 with a blurred-background fill.

    Layout (Premiere/Reels convention):
      1. Canvas = 1080x1920.
      2. Background layer: same source scaled by ``bg_scale`` (default 1.5x)
         to fill the canvas, heavily Gaussian-blurred (default sigma=40) so
         all detail washes out — just colour and motion.
      3. Foreground: 16:9 source scaled to fit width (1080px) with
         ``side_crop`` fraction trimmed off each side (default 10%), centred
         vertically. Black bars are masked by the blurred background
         showing through.

    *footage_ratio* is preserved for callers but is no longer used; the
    foreground sits centred in the canvas and the background fills the rest.
    """
    fg_w = OUT_WIDTH
    fg_h = round(SRC_HEIGHT * fg_w / SRC_WIDTH)
    if fg_h > OUT_HEIGHT:
        fg_h = OUT_HEIGHT

    # Trim side_crop fraction off each side of the foreground by scaling up
    # to (1/(1 - 2*side_crop)) and cropping back to fg_w.
    fg_scale = 1.0 / max(1.0 - 2.0 * side_crop, 0.1)
    fg_scaled_w = round(SRC_WIDTH * fg_scale)
    fg_scaled_h = round(SRC_HEIGHT * fg_scale)
    fg_y = (OUT_HEIGHT - fg_h) // 2
    crop_x = (fg_scaled_w - fg_w) // 2

    bg_w = round(OUT_WIDTH * bg_scale)
    bg_h = round(OUT_HEIGHT * bg_scale)

    _dbg("9x16", f"{src.name}: {SRC_WIDTH}x{SRC_HEIGHT} -> {OUT_WIDTH}x{OUT_HEIGHT} (fg {fg_w}x{fg_h}@y={fg_y} side-crop={side_crop:.0%}, bg {bg_w}x{bg_h} blur={blur_sigma})")

    # NVENC path
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex",
        (
            # Background: scale source up to bg_scale * canvas, crop centre
            f"[0:v]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
            f"crop={OUT_WIDTH}:{OUT_HEIGHT}:(iw-{OUT_WIDTH})/2:(ih-{OUT_HEIGHT})/2,"
            f"gblur=sigma={blur_sigma}:steps=3,format=yuv420p[bg];"
            # Foreground: scale to fill width+side_crop, crop centre, then pad
            # to canvas.
            f"[0:v]scale={fg_scaled_w}:{fg_scaled_h}:force_original_aspect_ratio=decrease,"
            f"crop={fg_w}:{fg_scaled_h}:{crop_x}:0,format=yuv420p[fg_raw];"
            f"color=black:s={OUT_WIDTH}x{OUT_HEIGHT}:d=1[blank];"
            f"[blank][fg_raw]overlay=x=0:y={fg_y}[fg];"
            # Composite fg over bg
            f"[bg][fg]overlay=0:0:format=auto[out]"
        ),
        "-map", "[out]",
        "-r", "64",
        "-fps_mode", "cfr",
        "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "0", "-cq", "20",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode == 0:
        mb = dst.stat().st_size / 1e6
        _dbg("composite", f"OK ({mb:.1f} MB)")
        return
        return

    _dbg("composite", f"[WARN] NVENC failed: {r.stderr[-300:]}")
    # CPU fallback (libx264)
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex",
        (
            f"[0:v]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
            f"crop={OUT_WIDTH}:{OUT_HEIGHT}:(iw-{OUT_WIDTH})/2:(ih-{OUT_HEIGHT})/2,"
            f"gblur=sigma={blur_sigma}:steps=4,format=yuv420p[bg];"
            f"[0:v]scale={fg_w}:-1,format=yuv420p[fg_raw];"
            f"color=black:s={OUT_WIDTH}x{OUT_HEIGHT}:d=1[blank];"
            f"[blank][fg_raw]overlay=x=0:y={fg_y}[fg];"
            f"[bg][fg]overlay=0:0:format=auto[out]"
        ),
        "-map", "[out]",
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",
        str(dst),
    ]
    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r2.returncode != 0:
        raise RuntimeError(f"Composite failed (NVENC + libx264): {r2.stderr[-500:]}")
    mb = dst.stat().st_size / 1e6
    _dbg("composite", f"OK CPU ({mb:.1f} MB)")


def render_shorts(
    timeline_path: Path,
    player: str | None = None,
    batch_size: int = 0,
    footage_ratio: int = 10,
) -> Path:
    """Render all shorts from a timeline JSON.
    
    Returns the output directory path.
    """
    tl = json.loads(timeline_path.read_text(encoding="utf-8"))
    shorts = tl.get("shorts", [])
    if not shorts:
        raise ValueError("No shorts in timeline")

    demo_path = Path(tl["demo_path"])
    if not demo_path.exists():
        raise FileNotFoundError(f"Demo not found: {demo_path}")

    out_dir = resolve_output_dir(demo_path, player=player)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments_dir = out_dir / "segments"
    # Clear any previous render's segments so we don't pick up stale
    # sequence-* files / dirs from a prior run.
    if segments_dir.exists():
        import shutil
        shutil.rmtree(segments_dir)
    segments_dir.mkdir(parents=True, exist_ok=True)

    print(f"Shorts: {len(shorts)} clips, map={tl.get('map', 'Unknown')}")
    print(f"Demo: {demo_path}")
    print(f"Output: {out_dir}")

    # Split into batches
    if batch_size > 0:
        batches = [shorts[i:i + batch_size] for i in range(0, len(shorts), batch_size)]
    else:
        batches = [shorts]

    print(f"  Batches: {len(batches)} (batch size: {batch_size if batch_size > 0 else 'all'})")

    for batch_idx, batch in enumerate(batches):
        batch_start = sum(len(b) for b in batches[:batch_idx])
        batch_end = batch_start + len(batch) - 1
        _dbg("batch", f"batch {batch_idx + 1}/{len(batches)}: shorts {batch_start + 1}-{batch_end + 1}")

        config = _build_csdm_config(batch, demo_path, segments_dir)
        conf_path = out_dir / f"batch_{batch_idx + 1}_config.json"
        conf_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        t0 = time.time()
        ret = _run_csdm(conf_path)
        elapsed = time.time() - t0
        if ret != 0:
            raise RuntimeError(f"CSDM batch {batch_idx + 1} failed (exit {ret}, {elapsed:.0f}s)")

        _dbg("csdm", f"batch {batch_idx + 1} rendered in {elapsed:.0f}s")

        seq_files = _find_sequence_files(segments_dir, len(batch), batch)
        if len(seq_files) != len(batch):
            _dbg("render", f"[WARN] Expected {len(batch)} sequence files, found {len(seq_files)}")

    # Composite each segment into 9:16
    seq_files = _find_sequence_files(segments_dir, len(shorts), shorts)
    if not seq_files:
        seq_files = sorted(segments_dir.rglob("video.mp4"))
        if not seq_files:
            raise RuntimeError(f"No rendered sequence files found in {segments_dir}")

    for i, (seg_file, short) in enumerate(zip(seq_files, shorts)):
        out_name = f"short_{i + 1:03d}.mp4"
        dst = out_dir / out_name
        if dst.exists() and dst.stat().st_size >= 1_048_576:
            w, h = _probe_resolution(dst)
            if w == OUT_WIDTH and h == OUT_HEIGHT:
                _dbg("composite", f"[SKIP] {out_name} already rendered at {OUT_WIDTH}x{OUT_HEIGHT}")
                continue
        _composite_9x16(seg_file, dst, footage_ratio=footage_ratio)
        w, h = _probe_resolution(dst)
        dur = _probe_duration(dst)
        _dbg("done", f"{out_name}: {w}x{h} {dur:.1f}s (pov: {short['pov_steam_id']}, type: {short['short_type']})")

    print(f"\nDone. Shorts in {out_dir}")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser(description="Render Short Timeline segments + composite to 9:16")
    ap.add_argument("timeline", type=Path, help="Path to short_timeline.json")
    ap.add_argument("--player", type=str, default=None, help="Steam ID for HLTV demo output dir")
    ap.add_argument("--output", "-o", type=Path, default=None, help="Override output directory")
    ap.add_argument("--batches", type=int, default=0,
                   help="Shorts per batch (0 = all in one)")
    ap.add_argument("--footage-ratio", type=int, default=10,
                   help="Footage proportion in 16-unit height (default 10 → 1200px)")
    args = ap.parse_args()

    try:
        render_shorts(args.timeline, player=args.player, batch_size=args.batches,
                       footage_ratio=args.footage_ratio)
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())