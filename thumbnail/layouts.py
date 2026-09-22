from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from thumbnail.generator import (
    WIDTH,
    HEIGHT,
    FONT_SIZES,
    FONT_PATH,
    AVATAR_HEIGHT_RATIO,
    TEXT_COLOR,
    cutout_player,
    draw_text,
    load_background,
    scale_player,
)

LINE_GAP = 1.15
LOGO_SLOT_TOP_GAP = 26  # px breathing room above the tournament logo


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


def _draw_overlay_badge(img: Image.Image, keyboard: bool = True) -> None:
    """Draw stacked pills in top-left corner for the overlay variant.

    With the keyboard input overlay on (legacy), the primary pill reads
    ``W/ INPUT OVERLAY`` (always-on keyboard state) and a secondary
    ``+ UTIL CAMS`` pill is stacked below. With keyboard off (default),
    a single ``W/ UTIL CAMS`` pill is drawn instead.
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

    if not keyboard:
        # Util-cams only: single pill.
        _draw_pill(
            draw,
            "W/ UTIL CAMS",
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


def _draw_text_scrim(img: Image.Image) -> None:
    """Soft dark horizontal gradient over the right portion behind the text.

    Makes white text pop on a busy background without a heavier stroke.
    """
    W, H = img.size
    left = int(W * 0.55)
    width = W - left
    if width <= 0:
        return
    ramp = Image.new("L", (width, 1))
    ramp.putdata([int(110 * (i / max(1, width - 1))) for i in range(width)])
    ramp = ramp.resize((width, H))
    black = Image.new("RGBA", (width, H), (0, 0, 0, 255))
    black.putalpha(ramp)
    img.paste(black, (left, 0), black)


def _draw_tournament_logo(
    img: Image.Image,
    logo_path: Path,
    center_x: int,
    center_y: int,
    max_width: int = 340,
) -> None:
    """Paste a tournament logo centered over the slot where its name would print,
    on a soft dark rounded panel so the (often transparent) logo stays readable
    over a busy background. ``center_y`` is the vertical center of the logo box.
    """
    try:
        logo = Image.open(logo_path).convert("RGBA")
        scale = min(1.0, max_width / logo.width)
        w = int(logo.width * scale)
        h = int(logo.height * scale)
        logo = logo.resize((w, h), Image.LANCZOS)

        # Soft dark rounded panel behind the logo.
        pad_x, pad_y = 22, 14
        panel_w = w + pad_x * 2
        panel_h = h + pad_y * 2
        panel_left = int(center_x - panel_w / 2)
        panel_top = int(center_y - panel_h / 2)
        base = img.convert("RGBA")
        draw_base = ImageDraw.Draw(base)
        draw_base.rounded_rectangle(
            [panel_left, panel_top, panel_left + panel_w, panel_top + panel_h],
            radius=16,
            fill=(0, 0, 0, 170),
        )
        base.alpha_composite(logo, (int(center_x - w / 2), int(center_y - h / 2)))
        img.paste(base.convert(img.mode), (0, 0))
    except Exception as e:
        print(f"  [WARN] tournament logo composite failed: {e}")


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
    keyboard: bool = False,
    tournament_logo: Path | None = None,
) -> Image.Image:
    bg = load_background(bg_path)

    player_img = cutout_player(avatar_path)
    target_h = int(HEIGHT * AVATAR_HEIGHT_RATIO)
    player_img = scale_player(player_img, target_h)

    pw, ph = player_img.size
    px = 48
    py = HEIGHT - ph + 40  # clear the overlay pills (hair was under badge)
    bg.paste(player_img, (px, py), player_img)

    _draw_text_scrim(bg)
    draw = ImageDraw.Draw(bg)

    text_x = int(WIDTH * 0.68)
    text_y_center = HEIGHT // 2

    kd_fill = (239, 195, 79)       # gold — hero K-D
    GOLD = FONT_SIZES["player"]  # same box as name -> even gaps above/below KD

    match_line = match_detail.strip()
    if map_name:
        match_line = f"{match_line}  ·  {map_name}" if match_line else map_name

    lines = [
        (player_name, FONT_SIZES["player"], TEXT_COLOR, 0),
        (kd, GOLD, kd_fill, 0),
        (match_line, FONT_SIZES["tiny"], TEXT_COLOR, 0),
    ]
    if tournament_logo is not None:
        lines.append((None, FONT_SIZES["tiny"], TEXT_COLOR, LOGO_SLOT_TOP_GAP))

    heights = [_line_height(s) for _, s, _f, _g in lines]
    total = sum(heights) + sum(g for _, _, _, g in lines)
    # anchor="mm" draws around the given CENTER, so y is the row middle.
    cursor = text_y_center - total // 2
    for (text, size, fill, gap), h in zip(lines, heights):
        cursor += gap
        if text is None and tournament_logo is not None:
            _draw_tournament_logo(bg, tournament_logo, text_x, cursor)
        else:
            draw_text(draw, text, text_x, cursor + h // 2, size, anchor="mm", fill=fill)
        cursor += h

    if variant == "overlay":
        _draw_overlay_badge(bg, keyboard=keyboard)

    return bg
