"""Shared full-HUD policy for Shorts and Hook: compact alive count + score blur.

Shorts and Hook used to render with ``cl_draw_only_deathnotices 1`` (killfeed
+ crosshair only, no spoiler possible). The product now keeps the full player
HUD with the compact alive-count team bar — ``5 vs 4`` numbers instead of the
wide per-player avatar row (which does not survive the 9:16 centre crop) — and
blurs just the two score digits in post so the result still leaks no score.

In-engine half (CSDM sequence cfg)::

    cl_draw_only_deathnotices 0
    cl_drawhud 1
    cl_teamcounter_playercount_instead_of_avatars true   (compact ``N vs N``)
    cl_hud_playercount_showcount 1                       (older alias, harmless)

Post half (ffmpeg, resolution-independent): the score row of the top-center
team bar is box-blurred. The bar's second row holds ONLY the two score
digits (timer + alive counts live on the first row), so one wide box over
row 2 can never touch them. The box is centre-anchored with fixed-pixel
offsets — the bar is centre-anchored at a fixed pixel size on every 1080p
capture, so fractions would only add cross-resolution drift.

Geometry is measured from a real 1728x1080 render (YEKINDAR mirage 4k):
score row sits ~41-80px below the top edge, -95…+45px around frame centre.
If a future HUD scale moves it, adjust ``SCORE_ROW_*`` here — both products
follow.
"""

from __future__ import annotations

# Compact alive-count team bar: numbers instead of the avatar row.
COMPACT_PLAYERCOUNT_CFG: list[str] = [
    "cl_teamcounter_playercount_instead_of_avatars true",
    "cl_hud_playercount_showcount 1",
]

# Resume-stamp value written next to rendered outputs so a HUD-policy change
# invalidates stale killfeed-only artifacts instead of silently reusing them.
# v2 added the chat kill (tv_nochat). v3 fixes it properly: the relayed
# "Console: ..." TextMsg prints are only killed by cl_showtextmsg 0 (plus
# hidehud 128) — tv_nochat/cl_chatfilters never did anything for them, so v2
# artifacts still carry baked-in chat and must be re-rendered, not recomposited.
HUD_POLICY = "full-hud+showcount+scoreblur+nochat-v3"

# Score-row geometry. Second row of the top-center team bar (scores only —
# timer and alive counts are on the first row above it). The box is centred
# on the frame middle with fixed-pixel offsets — the bar is centre-anchored
# at a fixed pixel size on every 1080p capture. Coordinates are computed in
# Python (delogo takes only literal pixels, no size expressions).
SCORE_ROW_X_OFF = 95
SCORE_ROW_W_PX = 140
SCORE_ROW_Y = 0.038
SCORE_ROW_H = 0.036


def score_blur_box(frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) pixel box over the score row for a *frame_w* x *frame_h*
    capture."""
    return (frame_w // 2 - SCORE_ROW_X_OFF,
            round(SCORE_ROW_Y * frame_h),
            SCORE_ROW_W_PX,
            round(SCORE_ROW_H * frame_h))


def score_blur_filter(src: str, dst: str, frame_w: int, frame_h: int,
                      tag: str = "sb") -> str:
    """Blur the score row of ``[src]`` into ``[dst]`` (one filter).

    ``src`` may be an input pad (``0:v``) or an intermediate label (without
    brackets). *frame_w* x *frame_h* is that stream's resolution — delogo
    takes literal pixels, so the box is computed here. *tag* is accepted
    for call-site stability but unused (no intermediate labels).

    Implemented as ``delogo`` (not crop/boxblur/overlay): a single
    full-frame filter, so ffmpeg's hardware negotiation treats it like
    every other stage. The crop-based variant downloaded a small odd-offset
    region off the CUDA frame and the blur came out half-green.
    """
    x, y, w, h = score_blur_box(frame_w, frame_h)
    return f"[{src}]delogo=x={x}:y={y}:w={w}:h={h}:show=0[{dst}]"
