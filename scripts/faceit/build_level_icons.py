"""Build the full FACEIT skill-level icon set (1-10) in one template.

Source of truth:
- levels 9 + 10: repo's assets/faceit/skill-level-*.png (filenames were
  swapped — the "9" file holds the red 10 and vice versa; fixed here),
  cleaned to real transparency.
- levels 1-8: synthesized in the identical template (dark disc + colored
  arc ring with bottom gap + number in ring color). Ring colors are
  Repeek's own level map, extracted from their extension bundle:
  1 grey #CCCCCC, 2-3 green #47e36e, 4-7 yellow #ffcd25, 8-9 orange
  #ff6c20, 10 red #ff2248. Arc geometry (radii, gap) is measured off the
  level-10 asset, not eyeballed.

Usage:
    python scripts/faceit/build_level_icons.py
Outputs:
    assets/faceit/levels/level-<n>.png   512x512 RGBA
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

ASSETS = PROJECT_ROOT / "assets" / "faceit"
OUT = ASSETS / "levels"
MONTSERRAT = ASSETS.parent / "fonts" / "Montserrat-Bold.ttf"

SIZE = 512
COLORS = {
    1: (204, 204, 204),
    2: (71, 227, 110), 3: (71, 227, 110),
    4: (255, 205, 37), 5: (255, 205, 37),
    6: (255, 205, 37), 7: (255, 205, 37),
    8: (255, 108, 32), 9: (255, 108, 32),
    10: (255, 34, 72),
}


def _clean_corners(src: Image.Image) -> Image.Image:
    """Corner-connected light surroundings -> transparent.

    The upstream exports sit on an opaque white/grey backdrop; only the
    corner-connected light region is background (a global luminance key
    would also eat anti-aliased ring edges).
    """
    im = src.convert("RGBA")
    w, h = im.size
    light = im.convert("L").point(lambda v: 255 if v > 185 else 0)
    for seed in ((5, 5), (w - 6, 5), (5, h - 6), (w - 6, h - 6)):
        if light.getpixel(seed) == 0:
            continue
        ImageDraw.floodfill(light, seed, 128, thresh=10)
    im.putalpha(light.point(lambda v: 0 if v == 128 else 255))
    return im


def _draw_number(draw: ImageDraw.ImageDraw, text: str, color: tuple,
                 cx: float, cy: float, target_h: float) -> None:
    size = 10
    font = ImageFont.truetype(str(MONTSERRAT), size)
    while True:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[3] - bbox[1] >= target_h or size > 400:
            break
        size += 4
        font = ImageFont.truetype(str(MONTSERRAT), size)
    draw.text((cx, cy), text, font=font, fill=color + (255,), anchor="mm")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Filenames are swapped upstream: the "9" file holds the red 10.
    red10 = _clean_corners(Image.open(ASSETS / "skill-level-9.png"))
    org9 = _clean_corners(Image.open(ASSETS / "skill-level-10.png"))
    red10.resize((SIZE, SIZE), Image.LANCZOS).save(OUT / "level-10.png")
    org9.resize((SIZE, SIZE), Image.LANCZOS).save(OUT / "level-9.png")
    print("level-9, level-10: cleaned + renamed")

    # Measured off the level-10 asset (disc spans ~8%..92% of canvas):
    # outer radius 0.39, ring 0.105 thick, gap at bottom.
    outer_f, thick_f = 0.39, 0.105
    disc = (27, 30, 35)
    for level in range(1, 9):
        color = COLORS[level]
        im = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        cx = cy = SIZE / 2
        r = outer_f * SIZE
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=disc + (255,))
        # PIL arc degrees run clockwise from 3 o'clock: 205..335 leaves
        # the gap centered at the bottom (verified visually).
        d.arc([cx - r, cy - r, cx + r, cy + r],
              start=205, end=335, fill=color + (255,),
              width=max(2, int(thick_f * SIZE)))
        _draw_number(d, str(level), color, cx, cy + SIZE * 0.02, SIZE * 0.34)
        im.save(OUT / f"level-{level}.png")
        print(f"level-{level}: synthesized")
    print(OUT)


if __name__ == "__main__":
    main()
