"""Shared full-HUD policy for Shorts and Hook: score blur without spoilers.

Shorts render the full player HUD with the per-player avatar roster bar and
strongly blur just the two score digits in post so the result still leaks
no score. Hook renders the compact alive-count team bar instead (``N vs N``
numbers — ``COMPACT_PLAYERCOUNT_CFG``), where the score row stands alone.

Post half (ffmpeg, resolution-independent): crop/boxblur/overlay over the
score cells. Independently extracted 1440x1080 flameZ frames have cell
separators at x682/720/758, top y34, and fading bottoms near y70. Avatar
sources at 1920x1080 (magixx Anubis/Cache and tom1jed Dust2) independently
confirm x922/960/998 and the same y bounds: fixed 38px cells, not width-scaled.
Other capture heights/aspects lack source-frame validation.
The compact bar's second row holds ONLY the two score digits (timer + alive counts live on
the first row), so one wide box over row 2 can never touch them there.

Boxes are centre-anchored with fixed-pixel x offsets — the bar is
centre-anchored at a fixed pixel size on every 1080p capture — and fractional
y so the pre-stretch squash (4:3 captures render squashed before the blur)
scales them with the frame. Coordinates are computed in Python (delogo takes
only literal pixels, no size expressions).
"""

from __future__ import annotations

# Compact alive-count team bar: numbers instead of the avatar row.
# Values are numeric: CS2's console does not parse "true"/"false" here, and
# the string form silently left avatars on (caught on the flameZ 4K short).
# The mirv force-apply bypasses cheat protection entirely (same mechanism as
# chat_hide's hidehud/screenmessage lines): a plain set on a cheats-off demo
# server is a silent no-op, and the sequence cfg is where it must land.
COMPACT_PLAYERCOUNT_CFG: list[str] = [
    "cl_teamcounter_playercount_instead_of_avatars 1",
    "mirv_cvar_hack cl_teamcounter_playercount_instead_of_avatars 1",
    "cl_hud_playercount_showcount 1",
]

# Resume-stamp value written next to rendered outputs so a HUD-policy change
# invalidates stale killfeed-only artifacts instead of silently reusing them.
# v2 added the chat kill (tv_nochat). v3 fixes it properly: the relayed
# "Console: ..." TextMsg prints are only killed by cl_showtextmsg 0 (plus
# hidehud 128) — tv_nochat/cl_chatfilters never did anything for them, so v2
# artifacts still carry baked-in chat and must be re-rendered, not recomposited.
# v4 fixes the compact playercount cvar value: "true" never parsed, so every
# v3 render shipped the full avatar bar (and the score blur overlapped it).
# v5 removes the launch-template reset: cs2_pov.cfg runs `exec autoexec`
# mid-sequence and the template forced ...instead_of_avatars 0, stomping the
# compact bar set earlier in the same cfg. The template no longer touches it.
# v6 narrows the shorts blur to the digit gutters (avatars stay, portraits
# untouched) after the frame-proven compact detour.
# v7 doubles every delogo pass: a single pass leaves high-contrast digits
# readable through the blur (frame-proven on the flameZ 4K short).
# v8 replaces blur with solid cover boxes: even doubled, delogo ghosts stay
# readable at zoom. One dark slate everywhere (cells are translucent).
# v9 back to real blur, done right: even-aligned crop + heavy boxblur +
# overlay-back per box (user vetoed the solid-box look). delogo ghosts and
# odd-offset CUDA chroma corruption both frame-proven.
# v10 widens the left gutter box 27→32px: independent audit caught the "0"'s
# right curve protruding ~3px past the old edge.
# v11 extends it to 714: the same auditor caught a residual sliver at the
# new edge under 5x zoom.
# v12 same-size cells per user spec (measured span center-44..center+40,
# top row 36, both 42x48): the old content-fitted asymmetric pair was wrong.
# v13 independently measures cell dividers on the supplied flameZ capture;
# v12's guessed span overlapped neighboring alive-count panels.
# v14 shorts force the avatar bar explicitly (plain + mirv): identical cfgs
# rendered compact or full depending on inherited game state, so the value
# is never left to defaults again.
HUD_POLICY = "full-hud+showcount+scoreblur+nochat-v14"

# Score-row geometry. Second row of the top-center team bar (scores only —
# timer and alive counts are on the first row above it). The box is centred
# on the frame middle with fixed-pixel offsets — the bar is centre-anchored
# at a fixed pixel size on every 1080p capture. Coordinates are computed in
# Python (delogo takes only literal pixels, no size expressions).
# NiKo Mirage hook source (1440x1080): dividers x682/720/758,
# y34..70. The old center-95 / 140px mask blurred TeSeS's portrait.
# These digit-cell bounds also stay safe when the full avatar HUD is present.
SCORE_ROW_X_OFF = 38
SCORE_ROW_W_PX = 76
SCORE_ROW_Y = 34 / 1080
SCORE_ROW_H = 36 / 1080

# Equal, top-aligned score cells: measured separator centers x682/720/758
# on 1440x1080 flameZ frames, top divider y33..34, fade ends near y70.
# Glyphs occupy x688..712 / 733..748, y42..58; timer ends above y27.
# Avatar-bar validation after explicit cvar reset: flameZ 1440x1080 and
# three native 1920x1080 sources (magixx Anubis/Cache, tom1jed Dust2).
# Native dividers x922/960/998: identical center offsets, not width-scaled.
# Other _ASPECTS have calculated bounds only, not source-frame evidence.
GUTTER_Y = 34 / 1080
GUTTER_H = 36 / 1080
GUTTER_A_X_OFF = -38
GUTTER_A_W_PX = 38
GUTTER_B_X_OFF = 0
GUTTER_B_W_PX = 38

# Strong-blur strength. boxblur validates radius against the cropped region
# (empirically min-dim/2-1 per plane in this build), so radii derive from
# each box — fixed maxima would fail small boxes and waste large ones. Power
# 3 iterates both planes. Even-aligned crops only: odd crop offsets corrupt
# the CUDA chroma plane (half-green failure, frame-proven). delogo — even
# doubled — leaves high-contrast digit ghosts readable (frame-proven).
BLUR_POWER = 3
BLUR_LUMA_MAX = 13
BLUR_CHROMA_MAX = 6


def _blur_strength(w: int, h: int) -> tuple[int, int]:
    """(luma_radius, chroma_radius) valid for a w×h crop, strongest allowed."""
    luma = max(1, min(BLUR_LUMA_MAX, min(w, h) // 2 - 1))
    cw, ch = w // 2, h // 2
    chroma = max(1, min(BLUR_CHROMA_MAX, min(cw, ch) // 2 - 1))
    return luma, chroma


def _even_floor(v: int) -> int:
    return v - (v % 2)


def _even_ceil(v: int) -> int:
    return v + (v % 2)


def score_blur_box(frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) pixel box over the score row for a *frame_w* x *frame_h*
    capture (compact bar)."""
    return (frame_w // 2 - SCORE_ROW_X_OFF,
            round(SCORE_ROW_Y * frame_h),
            SCORE_ROW_W_PX,
            round(SCORE_ROW_H * frame_h))


def score_blur_boxes(frame_w: int, frame_h: int, *,
                     compact: bool = True) -> list[tuple[int, int, int, int]]:
    """Blur boxes for a *frame_w* x *frame_h* capture.

    Compact bar: the single wide score-row box. Full avatar bar: the two
    digit-gutter boxes (hook keeps the compact path).
    """
    if compact:
        return [score_blur_box(frame_w, frame_h)]
    cx = frame_w // 2
    y = round(GUTTER_Y * frame_h)
    h = round(GUTTER_H * frame_h)
    return [(cx + GUTTER_A_X_OFF, y, GUTTER_A_W_PX, h),
            (cx + GUTTER_B_X_OFF, y, GUTTER_B_W_PX, h)]


def score_blur_filter(src: str, dst: str, frame_w: int, frame_h: int,
                      tag: str = "sb", *, compact: bool = True) -> str:
    """Strongly blur the score digits of ``[src]`` into ``[dst]``.

    ``src`` may be an input pad (``0:v``) or an intermediate label (without
    brackets). *frame_w* x *frame_h* is that stream's resolution — crop and
    overlay take literal pixels, so the boxes are computed here. *tag* is
    accepted for call-site stability but unused.

    Each box is cropped, heavily blurred and overlaid back (even-aligned
    throughout: odd crop offsets corrupt the CUDA chroma plane). delogo —
    even doubled — leaves high-contrast digit ghosts readable; drawbox fill
    reads as censorship. Strong boxblur in the digit gutters blurs only the
    scores while portraits, timer, HP, money and alive counts stay sharp.
    """
    boxes = [(_even_floor(x), _even_floor(y), _even_ceil(w), _even_ceil(h))
             for x, y, w, h in score_blur_boxes(frame_w, frame_h, compact=compact)]
    n = len(boxes)
    parts = [f"[{src}]split={n + 1}[a]" + "".join(f"[b{i}]" for i in range(n))]
    for i, (x, y, w, h) in enumerate(boxes):
        lr, cr = _blur_strength(w, h)
        parts.append(
            f"[b{i}]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={lr}:luma_power={BLUR_POWER}:"
            f"chroma_radius={cr}:chroma_power={BLUR_POWER}[bb{i}]")
    cur = "a"
    for i, (x, y, w, h) in enumerate(boxes):
        nxt = dst if i == n - 1 else f"{dst}{i}"
        parts.append(f"[{cur}][bb{i}]overlay={x}:{y}:format=yuv420[{nxt}]")
        cur = nxt
    return ";".join(parts)
