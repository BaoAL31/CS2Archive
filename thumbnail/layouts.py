from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from thumbnail.generator import (
    WIDTH,
    HEIGHT,
    FONT_SIZES,
    FONT_PATH,
    AVATAR_HEIGHT_RATIO,
    _load_font,
    cutout_player,
    draw_text,
    load_background,
    scale_player,
)

LINE_GAP = 1.15


def _line_height(size: int) -> int:
    return int(size * LINE_GAP)


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    *,
    left: int,
    top: int,
    padding_x: int = 18,
    padding_y: int = 10,
    corner_radius: int = 12,
    fill: tuple = (0, 0, 0, 200),
    text_fill: tuple = (255, 255, 255, 255),
) -> int:
    """Draw a single pill anchored to the left edge, return its bottom y."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    badge_w = text_w + padding_x * 2
    badge_h = text_h + padding_y * 2

    x0 = left
    y0 = top
    x1 = left + badge_w
    y1 = y0 + badge_h

    draw.rounded_rectangle([x0, y0, x1, y1], radius=corner_radius, fill=fill)
    draw.text(
        (x0 + badge_w // 2, y0 + badge_h // 2),
        text,
        font=font,
        fill=text_fill,
        anchor="mm",
    )
    return y1


def _draw_overlay_badge(img: Image.Image, *, pbdems2: bool = False) -> None:
    """Draw stacked pills in top-left corner for the overlay variant.

    When *pbdems2* is True, the primary pill reads ``W/ UTILITY CAMS``
    (PBDEMS2 demos lack keyboard input data — only utility throw PiP is present).
    Otherwise the primary pill reads ``W/ INPUT OVERLAY`` (always-on keyboard
    state) and a secondary ``+ UTIL CAMS`` pill is stacked below.
    """
    from PIL import ImageDraw, ImageFont

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    margin = 20
    pill_gap = 8

    try:
        font_main = ImageFont.truetype(str(FONT_PATH), 30)
        font_sub = ImageFont.truetype(str(FONT_PATH), 24)
    except Exception:
        font_main = ImageFont.load_default()
        font_sub = font_main

    if pbdems2:
        # PBDEMS2 → single pill, no keyboard overlay section.
        bbox = draw.textbbox((0, 0), "W/ UTILITY CAMS", font=font_main)
        main_h = (bbox[3] - bbox[1]) + 7 * 2
        _draw_pill(
            draw,
            "W/ UTILITY CAMS",
            font_main,
            left=margin,
            top=margin,
        )
        img.paste(overlay, (0, 0), overlay)
        return

    # Standard: two stacked pills.
    bbox1 = draw.textbbox((0, 0), "W/ INPUT OVERLAY", font=font_main)
    bbox2 = draw.textbbox((0, 0), "+ UTIL CAMS", font=font_sub)
    main_h = (bbox1[3] - bbox1[1]) + 7 * 2
    sub_h = (bbox2[3] - bbox2[1]) + 7 * 2

    main_top = margin
    sub_top = margin + main_h + pill_gap

    _draw_pill(
        draw,
        "W/ INPUT OVERLAY",
        font_main,
        left=margin,
        top=main_top,
    )

    _draw_pill(
        draw,
        "+ UTIL CAMS",
        font_sub,
        left=margin,
        top=sub_top,
        padding_x=14,
        padding_y=7,
        corner_radius=10,
    )

    img.paste(overlay, (0, 0), overlay)


def generate(
    bg_path: Path,
    avatar_path: Path,
    player_name: str,
    kd: str,
    rating: str,
    map_name: str,
    match_detail: str,
    tournament: str = "",
    stage: str = "",
    variant: str = "raw",
    *,
    pbdems2: bool = False,
) -> Image.Image:
    bg = load_background(bg_path)

    player_img = cutout_player(avatar_path)
    target_h = int(HEIGHT * AVATAR_HEIGHT_RATIO)
    player_img = scale_player(player_img, target_h)

    pw, ph = player_img.size
    px = 0
    py = HEIGHT - ph
    bg.paste(player_img, (px, py), player_img)

    draw = ImageDraw.Draw(bg)

    text_x = int(WIDTH * 0.68)
    text_y_center = HEIGHT // 2

    lines = [
        (player_name, FONT_SIZES["player"]),
        (f"K-D: {kd}", FONT_SIZES["stat"]),
        (f"Rating: {rating}", FONT_SIZES["stat"]),
        (map_name, FONT_SIZES["small"]),
        (match_detail, FONT_SIZES["small"]),
    ]
    if stage:
        lines.append((stage, FONT_SIZES["tiny"]))
    if tournament:
        lines.append((tournament, FONT_SIZES["tiny"]))

    total = sum(_line_height(s) for _, s in lines)
    current_y = text_y_center - total // 2

    for text, size in lines:
        draw_text(draw, text, text_x, current_y, size, anchor="mm")
        current_y += _line_height(size)

    if variant == "overlay":
        _draw_overlay_badge(bg, pbdems2=pbdems2)

    return bg


def generate_faceit(
    bg_path: Path,
    player_name: str,
    map_name: str,
    match_detail: str,
    tournament: str = "",
    variant: str = "raw",
    *,
    pbdems2: bool = False,
    avatar_path: Path | None = None,
) -> Image.Image:
    """FACEIT thumbnail: blurred kill-frame background + text overlay.

    Avatar is composited when available (mirrors the HLTV layout). FACEIT demos
    carry no HLTV-style ratings, so no K/D or Rating line is shown.
    """
    bg = load_background(bg_path)

    if avatar_path is not None:
        try:
            player_img = cutout_player(avatar_path)
            target_h = int(HEIGHT * AVATAR_HEIGHT_RATIO)
            player_img = scale_player(player_img, target_h)
            pw, ph = player_img.size
            px = 0
            py = HEIGHT - ph
            bg.paste(player_img, (px, py), player_img)
        except Exception as e:
            print(f"  [WARN] faceit avatar composite failed: {e}")

    draw = ImageDraw.Draw(bg)

    # FACEIT badge (top-left pill)
    _draw_pill(
        draw, "FACEIT CS2", FONT_SIZES["tiny"],
        left=int(WIDTH * 0.04), top=int(HEIGHT * 0.06),
        fill=(250, 90, 30, 220), text_fill=(255, 255, 255, 255),
    )

    text_x = int(WIDTH * 0.5)
    text_y_center = int(HEIGHT * 0.55)
    lines = [
        (player_name, FONT_SIZES["player"]),
        (map_name, FONT_SIZES["stat"]),
        (match_detail, FONT_SIZES["small"]),
    ]
    if tournament:
        lines.append((tournament, FONT_SIZES["tiny"]))

    total = sum(_line_height(s) for _, s in lines)
    current_y = text_y_center - total // 2
    for text, size in lines:
        draw_text(draw, text, text_x, current_y, size, anchor="mm")
        current_y += _line_height(size)

    if variant == "overlay":
        _draw_overlay_badge(bg, pbdems2=pbdems2)

    return bg
