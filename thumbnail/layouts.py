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
    right: int,
    top: int,
    padding_x: int = 18,
    padding_y: int = 10,
    corner_radius: int = 12,
    fill: tuple = (0, 0, 0, 200),
    text_fill: tuple = (255, 255, 255, 255),
) -> int:
    """Draw a single pill anchored to the right edge, return its bottom y."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    badge_w = text_w + padding_x * 2
    badge_h = text_h + padding_y * 2

    x0 = right - badge_w
    y0 = top
    x1 = right
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


def _draw_overlay_badge(img: Image.Image) -> None:
    """Draw stacked pills in bottom-right corner for the overlay variant.

    Primary: ``W/ INPUT OVERLAY`` (always-on keyboard state).
    Secondary: ``+ UTIL CAMS`` (sparse PiP utility throw flights).
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

    # Measure first to anchor stack to bottom edge.
    bbox1 = draw.textbbox((0, 0), "W/ INPUT OVERLAY", font=font_main)
    bbox2 = draw.textbbox((0, 0), "+ UTIL CAMS", font=font_sub)
    main_h = (bbox1[3] - bbox1[1]) + 7 * 2
    sub_h = (bbox2[3] - bbox2[1]) + 7 * 2

    right_edge = WIDTH - margin
    bottom = HEIGHT - margin

    # Stack from top: main (bigger) on top, sub (smaller) on bottom, with gap.
    # Compute absolute top of stack so total fits inside bottom margin.
    total_h = main_h + pill_gap + sub_h
    stack_top = bottom - total_h
    main_top = stack_top
    sub_top = stack_top + main_h + pill_gap

    # Primary pill on top
    _draw_pill(
        draw,
        "W/ INPUT OVERLAY",
        font_main,
        right=right_edge,
        top=main_top,
    )

    # Secondary pill on bottom
    _draw_pill(
        draw,
        "+ UTIL CAMS",
        font_sub,
        right=right_edge,
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
        _draw_overlay_badge(bg)

    return bg
