from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from thumbnail.generator import (
    WIDTH,
    HEIGHT,
    FONT_SIZES,
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


def generate(
    bg_path: Path,
    avatar_path: Path,
    player_name: str,
    kd: str,
    rating: str,
    map_name: str,
    match_detail: str,
    tournament: str = "",
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
    if tournament:
        lines.append((tournament, FONT_SIZES["tiny"]))

    total = sum(_line_height(s) for _, s in lines)
    current_y = text_y_center - total // 2

    for text, size in lines:
        draw_text(draw, text, text_x, current_y, size, anchor="mm")
        current_y += _line_height(size)

    return bg
