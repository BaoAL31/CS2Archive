from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import os
import tempfile

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

# Head-width normalization: HLTV bodyshots vary in framing (chest-up vs
# waist-up), so scaling by full-body height makes heads inconsistent
# (e.g. zont1x waist-up rendered ~75% of donk chest-up). Instead scale by the
# silhouette width at temple level (~10-25% below bbox top), which is stable
# across adults and framing-independent. Calibrated so donk.png renders at
# ~its current size (head_w 101px * 1.584 ~= 160).
TARGET_HEAD_WIDTH = 160
# Clamp so extreme framings (profile, caps, square headshots) can't blow
# past the frame or shrink to nothing. Height fallback when no head found.
MAX_AVATAR_HEIGHT = 700
MIN_AVATAR_HEIGHT = 480


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if FONT_PATH.exists():
        return ImageFont.truetype(str(FONT_PATH), size)
    return ImageFont.load_default()


def load_background(bg_path: Path) -> Image.Image:
    img = Image.open(bg_path).convert("RGB")
    # Cover-crop to 16:9 around the center — never stretch or pillarbox.
    # 4:3 POV frames (e.g. 1280x960 stretched captures) must fill the thumb
    # like native 16:9 footage instead of gaining black side bars.
    sw, sh = img.size
    target = WIDTH / HEIGHT
    if sw / sh > target:  # too wide -> crop x
        nw = max(1, int(sh * target))
        x0 = (sw - nw) // 2
        img = img.crop((x0, 0, x0 + nw, sh))
    elif sw / sh < target:  # too tall -> crop y
        nh = max(1, int(sw / target))
        y0 = (sh - nh) // 2
        img = img.crop((0, y0, sw, y0 + nh))
    return img.resize((WIDTH, HEIGHT), Image.LANCZOS)


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


def _estimate_head_width(player: Image.Image) -> float | None:
    """Median opaque width across temple-level rows (10-25% of bbox height).

    Front-facing bodyshot assumption: the topmost blob is the head. Returns
    None when the silhouette is unusable (too few opaque rows)."""
    alpha = player.getchannel("A") if player.mode == "RGBA" else None
    if alpha is None:
        return None
    w, h = player.size
    px = alpha.load()
    widths: list[int] = []
    for y in range(int(h * 0.10), int(h * 0.25)):
        xs = [x for x in range(w) if px[x, y] > 0]
        if xs:
            widths.append(xs[-1] - xs[0] + 1)
    if len(widths) < 5:
        return None
    widths.sort()
    return float(widths[len(widths) // 2])


def scale_player(player: Image.Image, target_height: int) -> Image.Image:
    player = _trim_transparent(player)
    w, h = player.size
    if h <= 0:
        return player
    ratio = target_height / h
    head_w = _estimate_head_width(player)
    if head_w and head_w > 0:
        ratio = min(TARGET_HEAD_WIDTH / head_w, MAX_AVATAR_HEIGHT / h)
        if h * ratio < MIN_AVATAR_HEIGHT:
            ratio = target_height / h  # framing too extreme, use height fallback
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))
    return player.resize((new_w, new_h), Image.LANCZOS)


def prepare_thumb_avatar(src: Path, dest: Path | None = None) -> Path:
    """Trim + crop to chest-up + head-width scale (HLTV bodyshot framing)."""
    player = _trim_transparent(cutout_player(Path(src)))
    w, h = player.size
    if h > 0 and w / h < 0.85:
        # Full-body / seated shots: drop legs/chair, keep head + torso.
        player = player.crop((0, 0, w, max(1, int(h * 0.62))))
    img = scale_player(player, int(HEIGHT * AVATAR_HEIGHT_RATIO))
    if dest is None:
        fd, name = tempfile.mkstemp(prefix="thumb_av_", suffix=".png")
        os.close(fd)
        dest = Path(name)
    else:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG")
    return dest


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
