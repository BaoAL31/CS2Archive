from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    return img


def cutout_player(avatar_path: Path) -> Image.Image:
    return Image.open(avatar_path).convert("RGBA")


def scale_player(player: Image.Image, target_height: int) -> Image.Image:
    w, h = player.size
    ratio = target_height / h
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    return player.resize((new_w, new_h), Image.LANCZOS)


def draw_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    font_size: int,
    anchor: str = "mm",
) -> None:
    font = _load_font(font_size)
    draw.text(
        (x + SHADOW_OFFSET, y + SHADOW_OFFSET), text, font=font,
        fill=SHADOW_COLOR, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )
    draw.text(
        (x, y), text, font=font,
        fill=TEXT_COLOR, anchor=anchor,
        stroke_width=STROKE_WIDTH, stroke_fill=SHADOW_COLOR,
    )
