from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from thumbnail.generator import (
    WIDTH,
    HEIGHT,
    FONT_SIZES,
    FONT_PATH,
    AVATAR_HEIGHT_RATIO,
    SHADOW_COLOR,
    SHADOW_OFFSET,
    STROKE_WIDTH,
    _load_font,
    cutout_player,
    draw_text,
    load_background,
    scale_player,
)

LINE_GAP = 1.15
ELO_TAG_SIZE_KEY = "tiny"
ELO_TAG_GAP = 6


def _line_height(size: int) -> int:
    return int(size * LINE_GAP)


def _elo_row_height(size: int) -> int:
    """Full height of the ELO row including the tag line beneath it.

    Keeps the same pitch as two normal rows so the following line (map name)
    stays in place; only the tag line's position inside the row changes.
    """
    return _line_height(size) + _line_height(FONT_SIZES[ELO_TAG_SIZE_KEY]) + ELO_TAG_GAP


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


def _draw_overlay_badge(img: Image.Image) -> None:
    """Draw stacked pills in top-left corner for the overlay variant.

    The primary pill reads ``W/ INPUT OVERLAY`` (always-on keyboard state)
    and a secondary ``+ UTIL CAMS`` pill is stacked below.
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


def _draw_text_custom(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font,
    fill: tuple,
    anchor: str = "mm",
) -> None:
    """Draw stroked text with a custom fill (draw_text is white-only)."""
    draw.text(
        (x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font,
        fill=SHADOW_COLOR, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )
    draw.text(
        (x, y), text, font=font, fill=fill, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )


def _draw_elo_line(
    draw: ImageDraw.ImageDraw,
    elo: int,
    opp_elo: int,
    x: int,
    y: int,
    font_size: int,
) -> None:
    """Two-tone ELO line: favorite (higher) red, underdog white.

    Layout::

        5512 vs 3740
         (ELO)   (AVG)

    ``(ELO)`` sits under ``vs`` and ``(AVG)`` under the opponent's average,
    both on their own tag line below the numbers. The AVG tag follows
    whichever number is the opponent's.
    """
    fav_fill = (250, 90, 30, 255)     # red/orange accent
    dog_fill = (255, 255, 255, 255)
    mid_fill = (220, 220, 220, 255)
    tag_fill = (235, 235, 235, 255)

    font = _load_font(font_size)
    tag_size = FONT_SIZES[ELO_TAG_SIZE_KEY]
    small_font = _load_font(tag_size)

    def _w(f, t: str) -> int:
        b = draw.textbbox((0, 0), t, font=f)
        return b[2] - b[0]

    mid = " vs "
    avg_tag = "(AVG)"
    elo_tag = "(ELO)"

    # Determine left/right strings + which side is the opponent.
    if elo >= opp_elo:
        left_str, right_str = str(elo), str(opp_elo)
        left_fill, right_fill = fav_fill, dog_fill
        avg_on_right = True
    else:
        left_str, right_str = str(opp_elo), str(elo)
        left_fill, right_fill = fav_fill, dog_fill
        avg_on_right = False

    wl, wm, wr = _w(font, left_str), _w(font, mid), _w(font, right_str)
    total = wl + wm + wr
    left = x - total // 2

    lx = left + wl // 2
    mx = left + wl + wm // 2
    rx = left + wl + wm + wr // 2

    _draw_text_custom(draw, left_str, lx, y, font, left_fill)
    _draw_text_custom(draw, mid, mx, y, font, mid_fill)
    _draw_text_custom(draw, right_str, rx, y, font, right_fill)

    # Tag line beneath the numbers: (ELO) under "vs", (AVG) under opponent.
    tag_y = y + font_size - ELO_TAG_GAP
    _draw_text_custom(draw, elo_tag, mx, tag_y, small_font, tag_fill)
    if avg_on_right:
        _draw_text_custom(draw, avg_tag, rx, tag_y, small_font, tag_fill)
    else:
        _draw_text_custom(draw, avg_tag, lx, tag_y, small_font, tag_fill)


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


def generate_faceit(
    bg_path: Path,
    player_name: str,
    map_name: str,
    elo: int | None = None,
    opp_elo: int | None = None,
    kd: str | None = None,
    variant: str = "raw",
    avatar_path: Path | None = None,
) -> Image.Image:
    """FACEIT thumbnail: blurred kill-frame background + text overlay.

    Avatar is composited when available (mirrors the HLTV layout). FACEIT
    demos carry no HLTV-style ratings, so instead of a ratings line the POV
    player's K/D and ELO vs the opposing team's average ELO are shown when
    provided (e.g. "38/9" then "5512 vs 3470").
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

    # Soft dark gradient behind the right text block so text pops on a busy bg.
    _draw_text_scrim(bg)

    text_x = int(WIDTH * 0.68)
    text_y_center = HEIGHT // 2
    lines = [
        (player_name, FONT_SIZES["player"]),
    ]
    if kd:
        lines.append((kd, FONT_SIZES["stat"]))
    elo_size = FONT_SIZES["stat"]
    if elo is not None and opp_elo is not None:
        lines.append((None, elo_size))
    lines.append((map_name, FONT_SIZES["small"]))

    total = sum(
        _elo_row_height(s) if text is None else _line_height(s)
        for text, s in lines
    )
    current_y = text_y_center - total // 2
    for text, size in lines:
        if text is None:
            _draw_elo_line(draw, elo, opp_elo, text_x, current_y, size)
            current_y += _elo_row_height(size)
        else:
            draw_text(draw, text, text_x, current_y, size, anchor="mm")
            current_y += _line_height(size)

    if variant == "overlay":
        _draw_overlay_badge(bg)

    return bg
