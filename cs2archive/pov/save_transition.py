"""Dissolve a save round into the next round (MoviePy CrossFade)."""
from __future__ import annotations

from pathlib import Path

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
