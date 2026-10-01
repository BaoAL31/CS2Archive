"""Prepend an intro-card segment to a finished POV video.

Flow:
  1. Render footage ENDING slightly past the first recorded round's start
     tick (read from ``--round-offsets`` sidecar, the same
     ``combined.round_offsets.json`` the overlay pipeline uses) via CSDM player
     mode: ``--seconds-before`` seconds of freeze-time/buy-phase lead-in plus
     ``--overlap-seconds`` of round-1 footage. The overlap exists because two
     independent CSDM/HLAE launches never land frame-exactly on the same tick
     (seek settle varies per render, typically 0-30 ticks) — tick-math
     abutment alone leaves a visible camera snap whenever the player moves the
     mouse near the joint.
  2. Content-align the footage tail against the head of ``--video`` (frame MSE
     on downscaled grayscale): locate the main video's first frame inside the
     footage and cut there, so the joint is truly seamless. Falls back to the
     tick-math cut when nothing matches clearly (static footage, overlay
     graphics over the head, probe failure).
  3. Slide the transparent left/right intro layers (``intro_left.png`` /
     ``intro_right.png``) in from the frame edges with an ease-in-out pop,
     hold, then slide them back out ending exactly at the aligned cut, so the
     footage is clean before the cut.

  4. Prepend the composed clip to ``--video``. Both are encoded with the same
     NVENC CQ 15 / 60M profile (the overlay final-export profile) so the concat
     is a stream copy — no re-encode of the main video, its audio is preserved.

Usage:
    python cs2archive/faceit/intro_prepend.py \
        --demo "demos/faceit/TeamA vs TeamB - dust2.dem" \
        --steam-id <steam64> \
        --video "youtube/<run>_overlay/video.mp4" \
        --intro "renders/pov-<stem>_<nick>/intro/intro.png" \
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

from cs2archive.config import settings  # noqa: E402
from cs2archive import encode

FFMPEG = settings.ffmpeg_exe
FFPROBE = settings.ffprobe_exe
CSDM = settings.csdm_cmd
# The POV render swaps the game's active autoexec.cfg to a render-specific one
# (autoexec_render.cfg) holding the POV player's crosshair/viewmodel/voice + name
# overrides, so the recorded footage matches the POV exactly. The intro footage
# must use the SAME autoexec or it won't match the POV render.
GAME_CFG = Path(r"D:\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg")
AUTOEXEC_MAIN = GAME_CFG / "autoexec.cfg"       # active cfg CS2 reads on startup
AUTOEXEC_RENDER = GAME_CFG / "autoexec_render.cfg"  # pre-render render-crosshair template

TICKRATE = 64  # CS2 demos on FACEIT are 64-tick

# Alignment tuning (see find_head_match): compared frames are downscaled to
# ALIGN_W x ALIGN_H grayscale; scores are mean-squared-error on 0-255 levels.
ALIGN_W, ALIGN_H = 320, 180
ALIGN_COMPARE_FRAMES = 30  # 0.5s of footage must agree, not just one frame


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

    Prefers explicit --native-width/--native-height; otherwise the same
    aspect-correct machine maximum the POV render uses (CR-17 — the pro's own
    resolution is ignored, how tall we can capture at their aspect ratio is
    what matters). The intro footage must render at this res to match the POV
    render, which is then upscaled to the final video size.
    """
    if w_override and h_override:
        return int(w_override), int(h_override)
    from cs2archive.capture_res import capture_size_for_aspect, capture_size_for_steam_id

    cap = capture_size_for_steam_id(steam_id) or capture_size_for_aspect("")
    return (int(w_override or cap[0]), int(h_override or cap[1]))


def render_footage(demo: Path, steam_id: str, render_dir: Path,
                   start_tick: int, seconds_before: float,
                   res: tuple[int, int, float],
                   player: str = "",
                   rename_map: dict | None = None,
                   voice_style: str = "swift",
                   hide_avatars: bool = False,
                   end_tick: int | None = None) -> Path:
    from types import SimpleNamespace
    from cs2archive.crosshair_code import effective_crosshair_height
    from cs2archive.crosshair_resolve import canonical_nick, resolve_crosshair_cvars
    from cs2archive.pov.render_pov import (
        CSDM, _sequence_cfg, _viewmodel_cvars_from_args, _voice_hud_session,
        _write_render_autoexec, rename_cfg_lines,
    )

    clip = render_dir / "intro_footage.mp4"
    if end_tick is None:
        end_tick = start_tick
    end_tick = max(end_tick, start_tick)
    w, h, fps = res
    player = canonical_nick(steam_id, (player or "").strip())
    xhair_h = effective_crosshair_height(h)
    cvars, info = resolve_crosshair_cvars(
        player, steam_id, demo, csdm_cmd=CSDM, screen_height=xhair_h,
    )
    cvars = list(cvars) + _viewmodel_cvars_from_args(SimpleNamespace(player=player))
    print(f"  [crosshair] {info.get('source')} @ {xhair_h}p ({len(cvars)} cvars)")
    rename_lines = rename_cfg_lines(rename_map)
    if rename_lines:
        print(f"  [rename] {len(rename_lines)} name override(s)")
    cfg_text = _sequence_cfg(cvars) + "\n".join(rename_lines) + "\n"
    cfg_path = render_dir / "intro_sequence.cfg"
    stamp = render_dir / "intro_footage.stamp"
    cfg_path.write_text(cfg_text, encoding="utf-8")
    # The stamp covers the game autoexec too (avatar policy lives only
    # there, not in the sequence cfg) so a policy flip re-renders.
    stamp_text = cfg_text + f"\nhide_avatars={int(bool(hide_avatars))}\n"
    start = max(0, int(start_tick - seconds_before * TICKRATE))
    # The stamp covers the tick span too: a changed overlap re-renders.
    stamp_text += f"start={start}\nend={int(end_tick)}\n"
    fresh = stamp.is_file() and stamp.read_text(encoding="utf-8") == stamp_text
    if clip.is_file() and clip.stat().st_size >= 1_048_576 and fresh:
        print(f"  [skip] footage exists: {clip.name}")
        return clip
    if clip.is_file():
        print(f"  [intro] dropping stale {clip.name} (crosshair cfg changed)")
        clip.unlink()
        (render_dir / "intro_segment.mp4").unlink(missing_ok=True)

    render_dir.mkdir(parents=True, exist_ok=True)
    # Same autoexec writer and Swift mount as the POV render.
    _write_render_autoexec(cvars, rename_map, None, hide_avatars=hide_avatars)
    _swap_autoexec(AUTOEXEC_RENDER)
    hud_args = SimpleNamespace(
        rename=json.dumps(rename_map or {}),
        voice_indicators=voice_style,
    )
    try:
        cmd = [
            CSDM, "video", str(demo.resolve()),
            str(start), str(int(end_tick)),
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
            "--cfg", str(cfg_path.resolve()),
            "--recording-system", "HLAE",
            "--close-game-after-recording",
            "--ffmpeg-executable-path", FFMPEG,
            "--ffmpeg-video-codec", "h264_nvenc",
            "--ffmpeg-crf", "15",
            "--ffmpeg-output-parameters="
            "-cq 15 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1",
        ]
        overlap_s = (int(end_tick) - start_tick) / TICKRATE
        print(f"  [render] csdm {start}->{int(end_tick)} "
              f"({seconds_before:g}s before round 1 + {overlap_s:.1f}s overlap, "
              f"{clip.name}) ...")
        # HLAE hook detection + retry: a hooked CS2 writes the clip; a failed
        # hook (vanilla demo viewer) writes nothing. Same protection as the
        # POV/util-cam renderers so a flaky hook retries instead of silently
        # producing garbage.
        from cs2archive.render.hook_aware import run_csdm_hook_aware
        with _voice_hud_session(str(demo), render_dir, steam_id, hud_args):
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
    stamp.write_text(stamp_text, encoding="utf-8")
    print(f"  [OK] footage: {produced} ({produced.stat().st_size/1e6:.0f} MB)")
    return produced


def build_intro_filter(
    final_res: tuple[int, int, float],
    seconds_before: float,
    pop_in: float,
    pop_out: float,
    left_rect: list[int],
    right_rect: list[int],
    fade_in: float = 0.0,
) -> str:
    w, h, fps = final_res
    duration = max(0.1, float(seconds_before))
    pin = max(0.05, float(pop_in))
    pout = max(0.05, float(pop_out))
    out_start = min(max(duration - pout, pin), duration)
    x0l = -(left_rect[0] + left_rect[2])
    x0r = w - right_rect[0]

    def ease(var: str) -> str:
        p = f"min(max({var},0),1)"
        return f"(pow({p},2)*(3-2*{p}))"

    ein = ease(f"t/{pin}")
    eout = ease(f"(t-{out_start})/{pout}")
    xl = f"if(lt(t,{pin}),{x0l}*(1-({ein})),if(lt(t,{out_start}),0,{x0l}*({eout})))"
    xr = f"if(lt(t,{pin}),{x0r}*(1-({ein})),if(lt(t,{out_start}),0,{x0r}*({eout})))"
    tail = f"[mid][right]overlay=x='{xr}':y=0:format=auto:eof_action=pass:shortest=1"
    if fade_in > 0:
        tail += f",fade=t=in:st=0:d={fade_in:g}"
    return (
        f"[0:v]scale={w}:{h}:flags=spline,setsar=1,fps={round(fps)}[base];"
        f"[1:v]format=rgba[left];"
        f"[2:v]format=rgba[right];"
        f"[base][left]overlay=x='{xl}':y=0:format=auto:eof_action=pass[mid];"
        f"{tail},format=nv12[v]"
    )


def _has_audio(path: Path) -> bool:
    import json as _json
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        return False
    try:
        return bool((_json.loads(r.stdout or "{}").get("streams") or []))
    except Exception:
        return False


def find_head_match(seg: list, main: list, expected: int,
                    window: int = 120,
                    compare: int = ALIGN_COMPARE_FRAMES,
                    rel_improve: float = 0.25,
                    abs_ceiling: float = 250.0) -> tuple[int, str]:
    """Locate ``main[0]`` inside ``seg`` (frame index) by MSE.

    ``seg`` is the tail of the intro footage, ``main`` the head of the POV
    video, both the same shape (grayscale downscaled frames). ``expected`` is
    the tick-math offset of ``main[0]`` inside ``seg``. The score of a
    candidate offset is the mean MSE over ``compare`` consecutive frames, so
    a single lucky frame cannot win.

    Returns ``(offset, method)`` where method is ``"aligned"`` when the best
    offset is decisively better than tick-math, else ``"tick-math"``. Static
    footage (everything matches) and poisoned footage (nothing matches —
    e.g. overlay graphics baked over the main head) both keep tick-math:
    the former needs no correction, the latter has no trustworthy one.
    """
    import numpy as np
    n_seg, n_main = len(seg), len(main)
    compare = max(1, min(int(compare), n_main, n_seg))
    lo = max(0, int(expected) - int(window))
    hi = min(n_seg - compare, int(expected) + int(window))
    if hi < lo or n_seg < compare or n_main < 1:
        return int(expected), "tick-math"

    def _score(o: int) -> float:
        acc = 0.0
        for k in range(compare):
            d = seg[o + k].astype(np.float32) - main[k].astype(np.float32)
            acc += float((d * d).mean())
        return acc / compare

    best_o, best_s = lo, _score(lo)
    for o in range(lo + 1, hi + 1):
        s = _score(o)
        if s < best_s:
            best_o, best_s = o, s
    exp_o = min(max(int(expected), lo), hi)
    exp_s = _score(exp_o)
    if abs(best_o - exp_o) <= 1:
        return exp_o, "tick-math"
    if best_s <= rel_improve * exp_s and best_s <= abs_ceiling:
        return best_o, "aligned"
    return exp_o, "tick-math"


def _extract_gray_frames(video: Path, ss: float, n: int, fps: float,
                         w: int = ALIGN_W, h: int = ALIGN_H) -> list:
    """Decode ``n`` grayscale frames from ``video`` at ``ss`` into ndarrays."""
    import numpy as np
    n = max(1, int(n))
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-ss", f"{max(0.0, float(ss)):.3f}", "-i", str(video),
        "-frames:v", str(n),
        "-vf", f"scale={int(w)}:{int(h)},fps={round(float(fps) or 60)}",
        "-f", "rawvideo", "-pix_fmt", "gray8", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode != 0 or not r.stdout:
        return []
    frame = int(w) * int(h)
    buf = r.stdout[: (len(r.stdout) // frame) * frame]
    return [np.frombuffer(buf[i * frame:(i + 1) * frame],
                          dtype=np.uint8).reshape(int(h), int(w))
            for i in range(len(buf) // frame)]


def _probe_duration(path: Path) -> float:
    import json as _json
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        return float((_json.loads(r.stdout or "{}").get("format") or {})
                     .get("duration") or 0.0)
    except (OSError, subprocess.TimeoutExpired, ValueError,
            KeyError, AttributeError):
        return 0.0


def align_footage_to_main(footage: Path, video: Path,
                          cut_sec: float, overlap_sec: float,
                          fps: float) -> tuple[float, str]:
    """Cut point (seconds from footage start) where ``video`` head matches.

    Returns ``(match_sec, method)``; method ``"tick-math"`` means the
    content match was inconclusive and the caller should cut at ``cut_sec``.
    Never raises: any probe/decode failure falls back to tick-math.
    """
    try:
        fps = float(fps) or 60.0
        f_dur = _probe_duration(footage)
        if f_dur <= 0:
            return float(cut_sec), "tick-math"
        tail = float(overlap_sec) + 1.0
        seg_ss = max(0.0, f_dur - tail)
        seg = _extract_gray_frames(footage, seg_ss, int(tail * fps) + 1, fps)
        main = _extract_gray_frames(video, 0.0, int(tail * fps) + 1, fps)
        if not seg or not main:
            return float(cut_sec), "tick-math"
        expected = int(round((float(cut_sec) - seg_ss) * fps))
        window = int(round(float(overlap_sec) * fps)) + 30
        off, method = find_head_match(seg, main, expected, window=window)
        match = seg_ss + off / fps
        lo = float(cut_sec) - float(overlap_sec) - 0.5
        hi = float(cut_sec) + float(overlap_sec) + 0.5
        if not (lo <= match <= hi):
            print(f"  [align] match {match:.2f}s outside "
                  f"[{lo:.2f},{hi:.2f}]s — keeping tick-math cut")
            return float(cut_sec), "tick-math"
        print(f"  [align] cut at {match:.2f}s (tick-math {float(cut_sec):.2f}s) "
              f"via {method}")
        return (match if method == "aligned" else float(cut_sec)), method
    except Exception as e:  # noqa: BLE001 — alignment is best-effort
        print(f"  [align] failed ({e}) — keeping tick-math cut")
        return float(cut_sec), "tick-math"


def compose_intro(footage: Path, intro: Path, out: Path,
                   native_res: tuple[int, int, float],
                   final_res: tuple[int, int, float],
                   seconds_before: float,
                   pop_in: float = 0.5, pop_out: float = 0.5,
                   fade_in: float = 0.0,
                   cut_sec: float | None = None) -> Path:
    """Compose the pane pop over ``footage``, cut at ``cut_sec``.

    ``cut_sec`` (seconds from footage start) is both the footage trim point
    (input ``-t``) and the pane slide-out anchor, so the panes always finish
    leaving exactly at the aligned joint. Defaults to ``seconds_before``
    (the tick-math cut, i.e. the pre-overlap behaviour).
    """
    if cut_sec is None:
        cut_sec = float(seconds_before)
    cut_sec = max(0.5, float(cut_sec))
    stamp = out.with_name(out.name + ".stamp")
    try:
        stamp_params = json.loads(stamp.read_text(encoding="utf-8")) if stamp.is_file() else {}
    except Exception:
        stamp_params = {}
    want_params = {"fade_in": fade_in, "seconds_before": seconds_before,
                   "pop_in": pop_in, "pop_out": pop_out,
                   "cut_sec": round(cut_sec, 3)}
    if out.is_file() and out.stat().st_size >= 1_048_576 and stamp_params == want_params:
        print(f"  [skip] composed segment exists: {out.name}")
        return out
    if out.is_file() and stamp_params != want_params:
        print(f"  [intro] dropping stale {out.name} (compose params changed)")
        out.unlink()
    fade_in = max(0.0, float(fade_in))
    if fade_in >= cut_sec:
        fade_in = max(cut_sec / 2.0, 0.0)

    left_path = intro.with_name("intro_left.png")
    right_path = intro.with_name("intro_right.png")
    details_path = intro.with_name("intro_details.json")
    missing = [p for p in (left_path, right_path, details_path) if not p.is_file()]
    if missing:
        raise SystemExit(
            "[ERROR] intro slide layers not found: "
            + ", ".join(str(p) for p in missing)
        )
    details = json.loads(details_path.read_text(encoding="utf-8"))
    w, h, fps = final_res
    tmp = out.with_name(out.name + ".part")
    fc = build_intro_filter(
        final_res, cut_sec, pop_in, pop_out,
        details["left_rect"], details["right_rect"],
        fade_in=fade_in,
    )
    # The head fade-in completes the hook's dip-to-black: hook tail fades out,
    # hard cut on black, intro head fades back in.
    audio_map: list[str] = ["-map", "0:a?"]
    if fade_in > 0 and _has_audio(footage):
        fc += (f";[0:a]afade=t=in:st=0:d={fade_in:g},aresample=48000,"
               f"aformat=sample_fmts=fltp:sample_rates=48000:"
               f"channel_layouts=stereo[a]")
        audio_map = ["-map", "[a]"]
    cmd = [
        FFMPEG, "-y",
        "-t", f"{cut_sec:.3f}", "-i", str(footage),
        "-loop", "1", "-i", str(left_path),
        "-loop", "1", "-i", str(right_path),
        "-filter_complex", fc,
        "-map", "[v]", *audio_map,
        *encode.codec_args(encode.FINAL),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-r", str(round(fps)), "-g", str(round(fps)), "-keyint_min", str(round(fps)),
        "-video_track_timescale", "15360",
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ]
    print(f"  [compose] intro pop over {footage.name} (cut {cut_sec:.2f}s) ...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not tmp.is_file():
        print((r.stderr or "")[-5000:])
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"[ERROR] compose failed (rc={r.returncode})")
    tmp.replace(out)
    stamp.write_text(json.dumps(want_params, indent=2), encoding="utf-8")
    print(f"  [OK] composed segment: {out.name}")
    return out


def _probe_audio(path: Path) -> tuple[bool, float]:
    """(has_audio, duration_seconds) via ffprobe."""
    import json as _json
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120)
        data = _json.loads(r.stdout or "{}")
        has_audio = any(s.get("codec_type") == "audio"
                        for s in data.get("streams", []))
        dur = float(data.get("format", {}).get("duration") or 0.0)
        return has_audio, dur
    except Exception:
        return False, 0.0


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
        # Video rides the concat demuxer as a stream copy (same NVENC profile
        # everywhere, no re-encode). Audio is decoded per-segment, normalized
        # and joined with the concat FILTER, then encoded once to the house
        # format — the two segments come from different encoders (CSDM MP3
        # 44.1k via the overlay remux, NVENC AAC 48k hook/intro), and joining
        # mixed audio through the concat demuxer does NOT transcode: with
        # `-c copy` it writes MP3 bytes under an AAC header (silence after
        # the join), and with `-c:a aac` the single decoder is fixed from the
        # first segment and the second segment's audio vanishes entirely.
        seg_audio, seg_dur = _probe_audio(segment)
        vid_audio, vid_dur = _probe_audio(video)
        abits: list[str] = []
        amaps: list[str] = []
        inputs: list[str] = ["-f", "concat", "-safe", "0", "-i", str(lst)]
        if seg_audio:
            inputs += ["-i", str(segment)]
            abits.append("[1:a]aresample=48000,aformat=sample_fmts=fltp:"
                         "sample_rates=48000:channel_layouts=stereo,"
                         "asetpts=PTS-STARTPTS[a0]")
            amaps.append("[a0]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{max(seg_dur, 0.1):.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            abits.append("[1:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                         "channel_layouts=stereo[a0]")
            amaps.append("[a0]")
        idx = 2
        if vid_audio:
            inputs += ["-i", str(video)]
            abits.append(f"[{idx}:a]aresample=48000,aformat=sample_fmts=fltp:"
                         "sample_rates=48000:channel_layouts=stereo,"
                         "asetpts=PTS-STARTPTS[a1]")
            amaps.append("[a1]")
        else:
            inputs += ["-f", "lavfi", "-t", f"{max(vid_dur, 0.1):.3f}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            abits.append(f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                         "channel_layouts=stereo[a1]")
            amaps.append("[a1]")
        # 20ms crossfade at the joint: no click even against segments rendered
        # before the baked dip-to-black fades existed.
        abits.append(f"{amaps[0]}{amaps[1]}acrossfade=d=0.02:c1=tri:c2=tri[aout]")
        cmd = [
            FFMPEG, "-y", *inputs,
            "-filter_complex", ";".join(abits),
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "256k",
            "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ]
        print(f"  [prepend] {segment.name} + {video.name} ...")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=14400)
        if r.returncode != 0 or not tmp.is_file():
            print((r.stderr or "")[-5000:])
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
    ap.add_argument("--overlap-seconds", type=float, default=2.0,
                     help="Seconds of round-1 footage rendered past the round-1 "
                          "start tick so the joint can be content-aligned "
                          "(separate CSDM/HLAE launches never land "
                          "frame-exactly on the same tick). 0 disables the "
                          "overlap (pure tick-math cut, legacy behaviour).")
    ap.add_argument("--native-width", type=int, default=None,
                    help="Native capture width of the POV render (e.g. 1280). "
                         "Defaults to the player's capture_width from player_accounts.json.")
    ap.add_argument("--native-height", type=int, default=None,
                    help="Native capture height of the POV render (e.g. 960).")
    ap.add_argument("--pop-in", type=float, default=0.5,
                    help="Pane slide-in duration (s)")
    ap.add_argument("--pop-out", type=float, default=0.5,
                    help="Pane slide-out duration (s)")
    ap.add_argument("--fade-in", type=float, default=0.0,
                    help="Fade-in from black at the segment head in seconds "
                         "(default: 0 — completes the hook's dip-to-black; "
                         "the pipeline passes --hook-fade here)")
    ap.add_argument("--player", default="",
                    help="POV nickname for the shared prosettings crosshair lookup")
    ap.add_argument("--rename", default="",
                    help="Same SteamID64->name JSON the POV render passes to mirv_replace_name")
    ap.add_argument("--voice-indicators", default="swift",
                    choices=("off", "swift"),
                    help="Match the POV render's speaker HUD (off mounts nothing)")
    ap.add_argument("--hide-avatars", action="store_true", default=False,
                    help="Hide scoreboard avatar images (auto-team lobbies with no "
                         "resolvable Steam avatars render them as missing-texture "
                         "checkers).")
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

    rename_map = json.loads(args.rename) if args.rename else {}
    overlap_sec = max(0.0, float(args.overlap_seconds))
    end_tick = start_tick + int(round(overlap_sec * TICKRATE))
    clip = render_footage(demo, args.steam_id, render_dir, start_tick,
                          args.seconds_before, (native_w, native_h, final_res[2]),
                          player=args.player, rename_map=rename_map,
                          voice_style=args.voice_indicators,
                          hide_avatars=args.hide_avatars,
                          end_tick=end_tick)
    # Content-align the joint: find the main video's first frame inside the
    # footage (which runs past the round-1 start tick by --overlap-seconds)
    # and cut there. Falls back to the tick-math cut on any failure.
    cut_sec, method = align_footage_to_main(
        clip, video, float(args.seconds_before), overlap_sec,
        final_res[2])
    segment = compose_intro(clip, intro, render_dir / "intro_segment.mp4",
                            (native_w, native_h, final_res[2]), final_res,
                            args.seconds_before,
                            args.pop_in, args.pop_out,
                            fade_in=args.fade_in,
                            cut_sec=cut_sec)
    prepend(segment, video, out)


if __name__ == "__main__":
    main()