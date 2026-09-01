"""Prepend an intro-card segment to a finished POV video.

Flow:
  1. Render ``--seconds-before`` seconds of footage ENDING at the first recorded
     round's start tick (read from ``--round-offsets`` sidecar, the same
     ``combined.round_offsets.json`` the overlay pipeline uses) via CSDM player
     mode. This is the freeze-time/buy-phase moment right before round 1, so it
     is a seamless lead-in to the existing video (its tick 0 == this clip's last
     tick).
  2. Composite the transparent intro card (``--intro intro.png``) over the clip
     with a quick fade-in pop, a hold, then a fade-out pop, so the footage is
     clean again before the cut.
  3. Prepend the composed clip to ``--video``. Both are encoded with the same
     NVENC CQ 15 / 60M profile (the overlay final-export profile) so the concat
     is a stream copy — no re-encode of the main video, its audio is preserved.

Usage:
    python scripts/faceit/intro_prepend.py \
        --demo "demos/faceit/TeamA vs TeamB - dust2.dem" \
        --steam-id <steam64> \
        --video "youtube/<run>_overlay/video.mp4" \
        --intro "renders/intro-<match>/intro.png" \
        --round-offsets "renders/pov-<stem>_<player>/combined.round_offsets.json" \
        --output "youtube/<run>_overlay/video_intro.mp4"

Resumable: an existing >=1MB footage clip is reused; an existing output skips
the compose step.

Steam must be running (CSDM/HLAE requirement).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from config import settings  # noqa: E402

FFMPEG = settings.ffmpeg_exe
CSDM = settings.csdm_cmd
CFG = Path(__file__).resolve().parents[2] / "assets" / "cs2_pov.cfg"

# The POV render swaps the game's active autoexec.cfg to a render-specific one
# (autoexec_render.cfg) holding the POV player's crosshair/viewmodel/voice + name
# overrides, so the recorded footage matches the POV exactly. The intro footage
# must use the SAME autoexec or it won't match the POV render.
GAME_CFG = Path(r"D:\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg")
AUTOEXEC_MAIN = GAME_CFG / "autoexec.cfg"       # active cfg CS2 reads on startup
AUTOEXEC_RENDER = GAME_CFG / "autoexec_render.cfg"  # pre-render render-crosshair template

TICKRATE = 64  # CS2 demos on FACEIT are 64-tick


def _swap_autoexec(src: Path) -> None:
    """Swap the game's active autoexec to ``src`` (render cfg for POV match).

    Mirrors ``render_pov._swap_autoexec``: CS2 reads autoexec.cfg at launch, so
    the render-specific crosshair/viewmodel/voice must be in the active file
    BEFORE the demo loads. The current autoexec.cfg is backed up so it can be
    restored afterwards.
    """
    import shutil
    if not src.exists():
        print(f"  [WARN] swap source missing: {src} — keeping current autoexec")
        return
    if AUTOEXEC_MAIN.exists():
        shutil.copy2(str(AUTOEXEC_MAIN), str(AUTOEXEC_MAIN) + ".intro_backup")
    shutil.copy2(str(src), str(AUTOEXEC_MAIN))
    print(f"  [autoexec] swapped {src.name} -> {AUTOEXEC_MAIN.name}")


def _restore_autoexec() -> None:
    """Restore the pre-intro autoexec.cfg after rendering."""
    import shutil
    backup = Path(str(AUTOEXEC_MAIN) + ".intro_backup")
    if backup.exists():
        shutil.copy2(str(backup), str(AUTOEXEC_MAIN))
        try:
            backup.unlink()
        except OSError:
            pass
        print(f"  [autoexec] restored prior autoexec")


def _probe_resolution(path: Path) -> tuple[int, int, float]:
    r = subprocess.run(
        [FFMPEG, "-i", str(path)], capture_output=True, text=True, timeout=120,
    )
    err = r.stderr or ""
    for line in err.splitlines():
        if "Video:" in line and "x" in line:
            import re
            m = re.search(r"(\d{3,5})x(\d{3,4})", line)
            fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
            if m:
                return int(m.group(1)), int(m.group(2)), (
                    float(fps_m.group(1)) if fps_m else 60.0
                )
    raise SystemExit(f"[ERROR] could not probe resolution of {path}")


def _first_round_start_tick(offsets_path: Path) -> int:
    data = json.loads(offsets_path.read_text(encoding="utf-8"))
    ticks = data.get("per_round_ticks", {}).get("1")
    if not ticks or not ticks[0]:
        raise SystemExit(
            f"[ERROR] no per_round_ticks[1] in {offsets_path}"
        )
    return int(ticks[0])


def _native_resolution(steam_id: str,
                       w_override: int | None, h_override: int | None) -> tuple[int, int]:
    """Native capture resolution of the POV render.

    Prefers explicit --native-width/--native-height; falls back to the player's
    ``capture_width``/``capture_height`` from player_accounts.json; defaults to
    1280x960. The intro footage must render at this native res to match the POV
    render (which is then upscaled to the final video size).
    """
    w = h = None
    try:
        import json as _json
        data = _json.loads(
            (Path(__file__).resolve().parents[2] / ".data" / "player_accounts.json")
            .read_text(encoding="utf-8")
        )
        players = data if isinstance(data, list) else data.get("players", [])
        for p in players:
            if str(p.get("steam_id")) == str(steam_id):
                w = p.get("capture_width")
                h = p.get("capture_height")
                break
    except Exception:
        pass
    w = w_override or w or 1280
    h = h_override or h or 960
    return int(w), int(h)


def render_footage(demo: Path, steam_id: str, render_dir: Path,
                   start_tick: int, seconds_before: float,
                   res: tuple[int, int, float]) -> Path:
    clip = render_dir / "intro_footage.mp4"
    if clip.is_file() and clip.stat().st_size >= 1_048_576:
        print(f"  [skip] footage exists: {clip.name}")
        return clip

    w, h, fps = res
    start = max(0, int(start_tick - seconds_before * TICKRATE))
    render_dir.mkdir(parents=True, exist_ok=True)
    # Mirror the POV render: use the render autoexec (b1t's crosshair/viewmodel/
    # voice + name overrides) so the intro footage matches the POV exactly.
    # assets/cs2_pov.cfg execs the game autoexec at launch. We render an
    # explicit tick range (start_tick - seconds_before -> start_tick) using
    # --focus-player to follow the POV player — NOT --mode player (which forces
    # --event and renders a whole round, not a bare tick window).
    _swap_autoexec(AUTOEXEC_RENDER)
    try:
        cmd = [
            CSDM, "video", str(demo.resolve()),
            str(start), str(start_tick),
            "--focus-player", steam_id,
            "--perspective", "player",
            "--no-show-x-ray",
            "--no-show-only-death-notices",
            "--show-assists",
            "--record-audio",
            "--player-voices",
            "--output", str(render_dir.resolve()),
            "--output-file-name", clip.name,
            "--framerate", str(int(fps)),
            "--width", str(w),
            "--height", str(h),
            "--cfg", str(CFG.resolve()),
            "--recording-system", "HLAE",
            "--close-game-after-recording",
            "--ffmpeg-executable-path", FFMPEG,
            "--ffmpeg-video-codec", "h264_nvenc",
            "--ffmpeg-crf", "15",
            "--ffmpeg-output-parameters="
            "-cq 15 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
        ]
        print(f"  [render] csdm {start}->{start_tick} "
              f"({int(seconds_before)}s before round 1, {clip.name}) ...")
        # HLAE hook detection + retry: a hooked CS2 writes the clip; a failed
        # hook (vanilla demo viewer) writes nothing. Same protection as the
        # POV/util-cam renderers so a flaky hook retries instead of silently
        # producing garbage.
        from hook_aware import run_csdm_hook_aware
        produced = run_csdm_hook_aware(
            cmd, "intro-footage", render_dir,
            hook_timeout=120.0, hook_retries=2,
        )
    except Exception as e:
        _restore_autoexec()
        raise SystemExit(f"[ERROR] intro footage render failed: {e}")
    finally:
        _restore_autoexec()
    if produced is None or produced.stat().st_size < 1_048_576:
        raise SystemExit("[ERROR] CSDM render failed to hook / no footage produced")
    print(f"  [OK] footage: {produced} ({produced.stat().st_size/1e6:.0f} MB)")
    return produced


def compose_intro(footage: Path, intro: Path, out: Path,
                  native_res: tuple[int, int, float],
                  final_res: tuple[int, int, float],
                  seconds_before: float,
                  pop_in: float = 0.7, pop_out: float = 1.0) -> Path:
    if out.is_file() and out.stat().st_size >= 1_048_576:
        print(f"  [skip] composed segment exists: {out.name}")
        return out

    w, h, fps = final_res       # final video size (e.g. 2560x1440)
    # Fade the card in over pop_in seconds, hold, fade out so the last ~pop_out
    # seconds are clean footage before the hard cut to the main video.
    fade_out_start = max(0.0, seconds_before - pop_out)
    tmp = out.with_name(out.name + ".part")
    # Upscale the footage to the final video res FIRST, then overlay the
    # full-resolution (2560x1440) card on top so it stays crisp — NOT downscaled
    # to native then re-upscaled (that's what made it look low-res).
    fc = (
        f"[0:v]scale={w}:{h}:flags=spline,setsar=1,fps={round(fps)}[base];"
        f"[1:v]format=rgba,"
        f"fade=t=in:st=0:d={pop_in}:alpha=1,"
        f"fade=t=out:st={fade_out_start}:d={pop_out}:alpha=1[ov];"
        f"[base][ov]overlay=0:0:format=auto:eof_action=pass,"
        f"format=nv12[v]"
    )
    cmd = [
        FFMPEG, "-y",
        "-i", str(footage),
        "-loop", "1", "-i", str(intro),
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "15",
        "-maxrate", "60M", "-bufsize", "120M",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-r", str(round(fps)), "-g", str(round(fps)), "-keyint_min", str(round(fps)),
        "-video_track_timescale", "15360",
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ]
    print(f"  [compose] intro pop over {footage.name} ...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not tmp.is_file():
        print((r.stderr or "")[-1500:])
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[ERROR] compose failed (rc={r.returncode})")
    tmp.replace(out)
    print(f"  [OK] composed segment: {out.name}")
    return out


def prepend(segment: Path, video: Path, output: Path) -> Path:
    if output.is_file() and output.stat().st_size >= 1_000_000:
        print(f"  [skip] output exists: {output.name}")
        return output
    import tempfile
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / "files.txt"
        lst.write_text(
            f"file '{segment.resolve()}'\nfile '{video.resolve()}'\n",
            encoding="utf-8",
        )
        tmp = output.with_name(output.name + ".part")
        cmd = [
            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ]
        print(f"  [prepend] {segment.name} + {video.name} ...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if r.returncode != 0 or not tmp.is_file():
            print((r.stderr or "")[-1500:])
            tmp.unlink(missing_ok=True)
            raise SystemExit(f"[ERROR] concat failed (rc={r.returncode})")
        tmp.replace(output)
    print(f"  [OK] output: {output}")
    return output


def main() -> None:
    ap = argparse.ArgumentParser(description="Prepend intro card to POV video")
    ap.add_argument("--demo", required=True, help="Path to .dem file")
    ap.add_argument("--steam-id", required=True, help="Steam64 of POV player")
    ap.add_argument("--video", required=True, help="Full render video.mp4")
    ap.add_argument("--intro", required=True, help="intro.png (transparent card)")
    ap.add_argument("--round-offsets", required=True,
                    help="combined.round_offsets.json sidecar")
    ap.add_argument("--output", required=True,
                    help="Final video (intro prepended)")
    ap.add_argument("--render-dir", default=None,
                    help="Where to put the rendered footage clip "
                         "(default: alongside --intro in a footage/ subdir)")
    ap.add_argument("--seconds-before", type=float, default=5.0,
                    help="Seconds of footage to render before round 1 start")
    ap.add_argument("--native-width", type=int, default=None,
                    help="Native capture width of the POV render (e.g. 1280). "
                         "Defaults to the player's capture_width from player_accounts.json.")
    ap.add_argument("--native-height", type=int, default=None,
                    help="Native capture height of the POV render (e.g. 960).")
    ap.add_argument("--pop-in", type=float, default=0.7,
                    help="Card fade-in duration (s)")
    ap.add_argument("--pop-out", type=float, default=1.0,
                    help="Card fade-out duration (s)")
    args = ap.parse_args()

    demo = Path(args.demo)
    video = Path(args.video)
    intro = Path(args.intro)
    offsets = Path(args.round_offsets)
    out = Path(args.output)

    if not demo.is_file():
        raise SystemExit(f"[ERROR] demo not found: {demo}")
    if not video.is_file():
        raise SystemExit(f"[ERROR] video not found: {video}")
    if not intro.is_file():
        raise SystemExit(f"[ERROR] intro not found: {intro}")
    if not offsets.is_file():
        raise SystemExit(f"[ERROR] round offsets not found: {offsets}")

    render_dir = (Path(args.render_dir) if args.render_dir
                  else intro.parent / "footage")
    render_dir.mkdir(parents=True, exist_ok=True)

    start_tick = _first_round_start_tick(offsets)
    final_res = _probe_resolution(video)  # 2560x1440 etc.
    native_w, native_h = _native_resolution(args.steam_id,
                                            args.native_width, args.native_height)
    print(f"  Round-1 start tick: {start_tick}")
    print(f"  native render {native_w}x{native_h} -> final {final_res[0]}x{final_res[1]}@{final_res[2]}")

    clip = render_footage(demo, args.steam_id, render_dir, start_tick,
                          args.seconds_before, (native_w, native_h, final_res[2]))
    segment = compose_intro(clip, intro, render_dir / "intro_segment.mp4",
                            (native_w, native_h, final_res[2]), final_res,
                            args.seconds_before,
                            args.pop_in, args.pop_out)
    prepend(segment, video, out)


if __name__ == "__main__":
    main()