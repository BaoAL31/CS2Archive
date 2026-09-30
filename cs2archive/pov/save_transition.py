"""Dissolve a save round into the next round (MoviePy CrossFade)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from cs2archive.config import settings

FADE_SECONDS = 1.2


def assemble_save_transition(
    save_mp4: Path,
    next_mp4: Path,
    out_mp4: Path,
    *,
    keep_seconds: float | None = None,
    fade_seconds: float = FADE_SECONDS,
) -> Path:
    """Keep ``keep_seconds`` of the save (or the whole clip), then dissolve into next round."""
    from moviepy import VideoFileClip, concatenate_videoclips, afx, vfx

    save_mp4 = Path(save_mp4)
    next_mp4 = Path(next_mp4)
    out_mp4 = Path(out_mp4)
    save = VideoFileClip(str(save_mp4))
    nxt = VideoFileClip(str(next_mp4))
    try:
        save_dur = float(save.duration or 0.0)
        next_dur = float(nxt.duration or 0.0)
        end_cap = max(0.0, save_dur - 0.04)
        outgoing_end = end_cap if keep_seconds is None else min(float(keep_seconds), end_cap)
        if outgoing_end <= fade_seconds * 2:
            raise ValueError(
                f"save clip too short to fade ({outgoing_end:.1f}s keep, {save_dur:.1f}s source)"
            )
        if next_dur <= fade_seconds * 2:
            raise ValueError(f"next-round clip too short ({next_dur:.1f}s)")
        outgoing = save.subclipped(0.0, outgoing_end)
        incoming = nxt.subclipped(0.0, min(next_dur, next_dur - 0.04))
        outgoing = outgoing.with_effects([vfx.CrossFadeOut(fade_seconds)])
        incoming = incoming.with_effects([vfx.CrossFadeIn(fade_seconds)])
        if outgoing.audio is not None:
            outgoing = outgoing.with_audio(
                outgoing.audio.with_effects([afx.AudioFadeOut(fade_seconds)])
            )
        if incoming.audio is not None:
            incoming = incoming.with_audio(
                incoming.audio.with_effects([afx.AudioFadeIn(fade_seconds)])
            )
        joined = concatenate_videoclips(
            [outgoing, incoming],
            method="compose",
            padding=-fade_seconds,
            bg_color=(0, 0, 0),
        )
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        joined.write_videofile(
            str(out_mp4),
            codec="h264_nvenc",
            audio_codec="aac",
            fps=save.fps or 60,
            ffmpeg_params=["-cq", "18", "-pix_fmt", "yuv420p", "-preset", "p4"],
            logger=None,
        )
        joined.close()
    finally:
        save.close()
        nxt.close()
    return out_mp4


def plan_dissolves(
    duration: float,
    junction_times: list[float],
    fade_seconds: float = FADE_SECONDS,
) -> list[float]:
    """Keep the junctions that have enough margin for a fade dissolve.

    Pure helper (no ffmpeg) so the margin logic is unit-testable. Every
    segment between consecutive bounds (file start, junctions, file end)
    must exceed ``fade_seconds`` or the xfade offset goes negative. Junctions
    that fail the margin keep their hard cut and are dropped from the result.
    """
    margin = fade_seconds + 0.05
    kept: list[float] = []
    for t in sorted(junction_times):
        if t < margin or t > duration - margin:
            continue
        if kept and t - kept[-1] < margin:
            continue
        kept.append(t)
    # The trailing segment past the last junction must also clear the margin;
    # dropping the last kept junction re-checks the one before it.
    while kept and duration - kept[-1] < margin:
        kept.pop()
    return kept


def dissolve_junctions(
    src: Path,
    junction_times: list[float],
    out: Path,
    *,
    fade_seconds: float = FADE_SECONDS,
) -> list[float]:
    """Dissolve in-file round junctions in ONE ffmpeg pass (xfade+acrossfade).

    Used by concat for obvious-defuse round ends: the alternative (re-encoding
    each junction separately and stream-copying the pieces back together)
    mixes encoder generations in one container — the MP3-seam fragility that
    muted round 9 on kyousuke/Mirage. A single uniform re-encode has no seams.

    Video: NVENC ``encode.DISSOLVE`` mezzanine (re-encoded again by scale +
    overlay later). Audio: normalized to AAC 48 kHz stereo — this also heals
    CSDM's MP3 stream-copy seams for the dissolved file. Returns the kept
    junction times (callers shift later round offsets by ``-fade`` each).
    """
    from cs2archive import encode

    src = Path(src)
    out = Path(out)
    duration = _probe_duration(src)
    kept = plan_dissolves(duration, junction_times, fade_seconds)
    if not kept:
        raise ValueError("no dissolvable junctions (all fail the fade margin)")
    n = len(kept)
    bounds = [0.0] + kept + [duration]
    seg_lens = [bounds[i + 1] - bounds[i] for i in range(n + 1)]

    parts: list[str] = []
    for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
        src_idx = i % 2  # same file twice so no stream is trimmed twice
        parts.append(
            f"[{src_idx}:v]trim=start={a:.6f}:end={b:.6f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        parts.append(
            f"[{src_idx}:a]atrim=start={a:.6f}:end={b:.6f},asetpts,"
            f"aresample=48000,aformat=channel_layouts=stereo[a{i}]"
        )
    cur_v = "v0"
    offset = seg_lens[0] - fade_seconds
    for i in range(1, n + 1):
        nxt_v = f"x{i}"
        parts.append(
            f"[{cur_v}][v{i}]xfade=transition=fade:duration={fade_seconds}:"
            f"offset={offset:.6f}[{nxt_v}]"
        )
        cur_v = nxt_v
        offset += seg_lens[i] - fade_seconds
    cur_a = "a0"
    for i in range(1, n + 1):
        nxt_a = f"m{i}"
        parts.append(f"[{cur_a}][a{i}]acrossfade=d={fade_seconds}[{nxt_a}]")
        cur_a = nxt_a
    fc = ";".join(parts)

    cmd = [
        settings.ffmpeg_exe, "-y", "-i", str(src), "-i", str(src),
        "-filter_complex", fc,
        "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
        *encode.codec_args(encode.DISSOLVE),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-level", "5.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
    if r.returncode != 0:
        out.unlink(missing_ok=True)
        print(f"  [dissolve] NVENC failed, retrying CPU: {r.stderr[-300:]}")
        cmd = [
            settings.ffmpeg_exe, "-y", "-i", str(src), "-i", str(src),
            "-filter_complex", fc,
            "-map", f"[{cur_v}]", "-map", f"[{cur_a}]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=6 * 3600)
        if r.returncode != 0:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"dissolve concat failed: {r.stderr[-600:]}")
    print(f"  [dissolve] {n} junction(s) dissolved (fade={fade_seconds}s) -> {out.name}")
    return kept


def _probe_duration(path: Path) -> float:
    import json

    r = subprocess.run(
        [settings.ffprobe_exe, "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr[-200:]}")
    try:
        duration = float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError(f"ffprobe returned no duration for {path}")
    return duration
