"""imgutil: rounded corners must be anti-aliased (no stair-stepped pixels)."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from imgutil import (  # noqa: E402
    drop_shadow,
    rounded_alpha_mask,
    rounded_layer,
)


def test_rounded_alpha_mask_is_anti_aliased():
    mask = rounded_alpha_mask((200, 120), radius=24)
    vals = set(mask.getdata())
    assert 255 in vals
    assert 0 in vals
    # Anti-aliasing means partial coverage values along the arc.
    partial = [v for v in vals if 0 < v < 255]
    assert partial, "rounded mask has no intermediate alpha -> aliased corners"


def test_rounded_layer_rounds_and_keeps_content():
    img = Image.new("RGB", (200, 120), (200, 10, 10))
    out = rounded_layer(img, radius=24)
    assert out.mode == "RGBA"
    assert out.size == (200, 120)
    # centre opaque, corners clear
    assert out.getpixel((100, 60))[3] == 255
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((199, 119))[3] == 0


def test_rounded_layer_supersample_zero_radius_is_full():
    img = Image.new("RGB", (40, 40), (1, 2, 3))
    out = rounded_layer(img, radius=0)
    assert out.getpixel((0, 0))[3] == 255


def test_drop_shadow_falls_bottom_right():
    from PIL import ImageDraw

    pane = Image.new("RGBA", (120, 80), (200, 0, 0, 255))
    alpha = Image.new("L", (120, 80), 0)
    ImageDraw.Draw(alpha).rounded_rectangle([20, 20, 99, 59], radius=12, fill=255)
    pane.putalpha(alpha)
    shadow, pad = drop_shadow(pane, offset=(8, 8), blur=14, spread=0, opacity=0.3)

    assert shadow.mode == "RGBA"
    assert shadow.size == (120 + 2 * pad, 80 + 2 * pad)
    right = shadow.getpixel((pad + 107, pad + 40))
    bottom = shadow.getpixel((pad + 60, pad + 67))
    left = shadow.getpixel((pad + 12, pad + 40))
    top = shadow.getpixel((pad + 60, pad + 12))

    assert right[:3] == bottom[:3] == left[:3] == top[:3] == (0, 0, 0)
    assert right[3] > left[3] * 3
    assert bottom[3] > top[3] * 3
    assert max(right[3], bottom[3]) > 0
