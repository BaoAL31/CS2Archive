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

SRC_WIDTH = 1920
SRC_HEIGHT = 1080
OUT_WIDTH = 1080
OUT_HEIGHT = 1920
CSDM_RECORD_FRAMERATE = 60

KILLFEED_CROP_W = 300
KILLFEED_CROP_H = 170
KILLFEED_CROP_X = SRC_WIDTH - KILLFEED_CROP_W
KILLFEED_CROP_Y = 10


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


def _render_kill_feed_pip(src: Path, dst: Path) -> None:
    """Pre-render the kill-feed PiP from the source segment.

    Crops the top band of the source (KILLFEED_CROP_W x KILLFEED_CROP_H at
    y=0) at native resolution — no rescaling, so no resampling artifacts.
    Encode is GPU (h264_nvenc). Single ffmpeg call; the result is fed into
    ``_composite_9x16`` as input [1:v] so the final composite remains a
    single encode pass.
    """
    vf = f"crop={KILLFEED_CROP_W}:{KILLFEED_CROP_H}:{KILLFEED_CROP_X}:{KILLFEED_CROP_Y}"
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(dst),
    ]

    _dbg("kf", f"{src.name}: crop {KILLFEED_CROP_W}x{KILLFEED_CROP_H}@{KILLFEED_CROP_X},{KILLFEED_CROP_Y} (native, no scale)")
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
    scale: float = 2.0,
    blur_radius: int = 50,
    darken_factor: float = 1.0,
    kill_feed: bool = True,
    kill_feed_path: Path | None = None,
    pip_scale: float = 1.0,
) -> None:
    """Composite a 1920x1080 source into 1080x1920 via ffmpeg filter chain.

    Background: source scaled to fill canvas (force_original_aspect_ratio=increase,
    crop to 1080x1920), Gaussian-blurred.

    Foreground: source scaled to fit canvas (force_original_aspect_ratio=decrease).
    ``scale=1.0`` fits the source inside without cropping.
    ``scale>1.0`` zooms in (e.g. 1.5 = source scaled to 1.5x, cropped centre to 1080).

    Kill-feed PiP (when ``kill_feed=True``, default): direct native crop from
    the source (no per‑PiP scaling) overlaid at top‑right of the foreground
    footage. Carved from [0:v] so it sits on the same source timeline as
    bg/fg — downstream overlays are on the final 1080×1920 canvas.

    Encode: NVIDIA NVENC (h264_nvenc) for GPU-accelerated H.264.
    Decode: cuda hwaccel for GPU-accelerated source decode.
    Audio: passthrough copy from the source.
    """
    gblur_sigma = max(1, blur_radius // 2)

    _dbg("9x16", f"{src.name}: 1080x1920 canvas, scale={scale}x, blur_sigma={gblur_sigma}")

    if scale == 1.0:
        fg_chain = (
            f"[fg_src]scale={OUT_WIDTH}:{OUT_HEIGHT}:"
            f"force_original_aspect_ratio=decrease[fg]"
        )
        fg_top = 0
    else:
        fg_chain = (
            f"[fg_src]scale={round(OUT_WIDTH * scale)}:-1,"
            f"crop={OUT_WIDTH}:ih[fg]"
        )
        fg_w = round(OUT_WIDTH * scale)
        fg_h = round(fg_w * SRC_HEIGHT / SRC_WIDTH)
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
            FFMPEG, "-y", "-hwaccel", "cuda",
            "-i", str(src)
        ]

        if kill_feed_path:
            cmd.extend(["-i", str(kill_feed_path)])
            pip_scale_chain = f"[1:v]scale={scaled_pip_w}:{scaled_pip_h}[scaled_pip];" if actual_pip_scale != 1.0 else ""
            pip_in = "[scaled_pip]" if actual_pip_scale != 1.0 else "[1:v]"
            filter_str = (
                f"[0:v]split=2[bg_src][fg_src];"
                f"{bg_chain};"
                f"{fg_chain};"
                f"{pip_scale_chain}"
                f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[tmp];"
                f"[tmp]{pip_in}overlay={pip_x}:{pip_y}[out]"
            )
        else:
            pip_chain = (
                f"[pip_src]crop={pip_w}:{pip_h}:"
                f"{KILLFEED_CROP_X}:{KILLFEED_CROP_Y}"
            )
            if actual_pip_scale != 1.0:
                pip_chain += f",scale={scaled_pip_w}:{scaled_pip_h}"
            pip_chain += "[pip];"
            
            filter_str = (
                f"[0:v]split=3[bg_src][fg_src][pip_src];"
                f"{bg_chain};"
                f"{fg_chain};"
                f"{pip_chain}"
                f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[tmp];"
                f"[tmp][pip]overlay={pip_x}:{pip_y}[out]"
            )

        cmd.extend([
            "-filter_complex", filter_str,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14", "-b:v", "0",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-level", "4.2",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ])
    else:
        vf = (
            f"[0:v]split=2[bg_src][fg_src];"
            f"{bg_chain};"
            f"{fg_chain};"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[out]"
        )
        cmd = [
            FFMPEG, "-y", "-hwaccel", "cuda", "-i", str(src),
            "-filter_complex", vf,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "h264_nvenc", "-preset", "p7", "-cq", "14", "-b:v", "0",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-level", "4.2",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dst),
        ]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg composite failed (rc={r.returncode}): {r.stderr[-2000:]}"
        )

    mb = dst.stat().st_size / 1e6
    _dbg("composite", f"OK ({mb:.1f} MB)")


def render_shorts(
    timeline_path: Path,
    player: str | None = None,
    batch_size: int = 0,
    scale: float = 2.0,
    pip_scale: float = 1.0,
    composite_only: bool = False,
    name: str | None = None,
) -> Path:
    """Render all shorts from a timeline JSON.
    
    When *composite_only* is True, skip CSDM and re-composite existing
    segments with new editing parameters (e.g. scale).
    
    Returns the output directory path.
    """
    tl = json.loads(timeline_path.read_text(encoding="utf-8"))
    shorts = tl.get("shorts", [])
    if not shorts:
        raise ValueError("No shorts in timeline")

    demo_path = Path(tl["demo_path"])
    if not demo_path.exists():
        raise FileNotFoundError(f"Demo not found: {demo_path}")

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

    # Split into batches
    if batch_size > 0:
        batches = [shorts[i:i + batch_size] for i in range(0, len(shorts), batch_size)]
    else:
        batches = [shorts]

    print(f"  Batches: {len(batches)} (batch size: {batch_size if batch_size > 0 else 'all'})")

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
        if name:
            base = name
        else:
            st = short["short_type"]
            nick = short.get("pov_nick", "unknown")
            tick = short.get("start_tick", 0)
            base = f"{st}-{nick}-t{tick}"
        out_name = f"{base}.mp4"
        dst = out_dir / out_name
        if dst.exists() and dst.stat().st_size >= 1_048_576:
            w, h = _probe_resolution(dst)
            if w == OUT_WIDTH and h == OUT_HEIGHT:
                _dbg("composite", f"[SKIP] {out_name} already rendered at {OUT_WIDTH}x{OUT_HEIGHT}")
                continue
        kf_path = segments_dir / f"{seg_file.stem}-kill_feed.mp4"
        if not kf_path.exists() or kf_path.stat().st_size < 1_048_576:
            _render_kill_feed_pip(seg_file, kf_path)
        _composite_9x16(seg_file, dst, scale=scale, kill_feed_path=kf_path, pip_scale=pip_scale)
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
    ap.add_argument("--scale", type=float, default=2.0,
                   help="Foreground scale multiplier (1.0 = fit canvas, 2.0 = 2x zoom with centre crop)")
    ap.add_argument("--pip-scale", type=float, default=0.0,
                   help="Kill feed scale multiplier (0.0 = auto-fill top banner height)")
    ap.add_argument("--composite-only", action="store_true",
                   help="Skip CSDM render; re-composite existing segments with new editing params")
    ap.add_argument("--name", type=str, default=None,
                   help="Output filename (without .mp4). Default: {short_type}-{pov_nick}")
    args = ap.parse_args()

    try:
        render_shorts(args.timeline, player=args.player, batch_size=args.batches,
                       scale=args.scale, pip_scale=args.pip_scale, composite_only=args.composite_only,
                       name=args.name)
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())