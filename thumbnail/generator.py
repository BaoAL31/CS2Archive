from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).parent.parent / "assets" / "fonts" / "Montserrat-Bold.ttf"

WIDTH, HEIGHT = 1280, 720

FONT_SIZES = {
    "player": 114,
    "stat": 75,
    "small": 54,
    "tiny": 40,
}

TEXT_COLOR = (255, 255, 255)
SHADOW_COLOR = (0, 0, 0)
SHADOW_OFFSET = 3
STROKE_WIDTH = 2

AVATAR_HEIGHT_RATIO = 0.90


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if FONT_PATH.exists():
        return ImageFont.truetype(str(FONT_PATH), size)
    return ImageFont.load_default()


def load_background(bg_path: Path) -> Image.Image:
    img = Image.open(bg_path).convert("RGB")
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    return img


def cutout_player(avatar_path: Path) -> Image.Image:
    return Image.open(avatar_path).convert("RGBA")


def _trim_transparent(player: Image.Image, alpha_min: int = 8) -> Image.Image:
    """Crop to the opaque subject so scale/paste ignore empty canvas padding.

    HLTV bodyshots keep a 400x417 canvas after rembg. Players framed smaller
    in that box (more left/side padding) otherwise look shrunk and shifted
    right, because scale uses the full canvas height and paste anchors at x=0.
    """
    if player.mode != "RGBA":
        player = player.convert("RGBA")
    alpha = player.getchannel("A")
    if alpha_min > 0:
        alpha = alpha.point(lambda p, t=alpha_min: 255 if p >= t else 0)
    bbox = alpha.getbbox()
    if not bbox:
        return player
    return player.crop(bbox)


def scale_player(player: Image.Image, target_height: int) -> Image.Image:
    player = _trim_transparent(player)
    w, h = player.size
    if h <= 0:
        return player
    ratio = target_height / h
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))
    return player.resize((new_w, new_h), Image.LANCZOS)


def draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font_size: int,
    anchor: str = "mm",
    fill: tuple = TEXT_COLOR,
) -> None:
    font = _load_font(font_size)
    draw.text(
        (x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font,
        fill=SHADOW_COLOR, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )
    draw.text(
        (x, y), text, font=font,
        fill=fill, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )
