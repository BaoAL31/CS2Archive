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


def _draw_overlay_badge(img: Image.Image) -> None:
    """Draw a semi-transparent pill badge in top-right corner for the overlay variant."""
    from PIL import ImageDraw, ImageFont

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    badge_text = "W/ INPUT OVERLAY"
    padding_x, padding_y = 18, 10
    corner_radius = 12
    margin = 20

    try:
        font = ImageFont.truetype(str(FONT_PATH), 30)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), badge_text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    badge_w = text_w + padding_x * 2
    badge_h = text_h + padding_y * 2

    x0 = WIDTH - badge_w - margin
    y0 = margin
    x1 = WIDTH - margin
    y1 = y0 + badge_h

    # Semi-transparent dark background pill
    draw.rounded_rectangle(
        [x0, y0, x1, y1],
        radius=corner_radius,
        fill=(0, 0, 0, 200),
    )

    # White text, centered in pill
    text_x = x0 + badge_w // 2
    text_y = y0 + badge_h // 2
    draw.text(
        (text_x, text_y),
        badge_text,
        font=font,
        fill=(255, 255, 255, 255),
        anchor="mm",
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
