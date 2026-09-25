"""Shared Pillow compositing helpers.

Anti-aliased rounded corners are the default here: ``ImageDraw.rounded_rectangle``
draws aliased edges, so every rounded mask/layer in this project should go
through these helpers instead of drawing a rounded rect directly.

Usage:
    from imgutil import rounded_layer, rounded_alpha_mask
    dst.alpha_composite(rounded_layer(img, radius=28), (x, y))
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

# Supersample factor for anti-aliasing; 4 is plenty for 1440p-scale radii.
ROUNDED_SUPERSAMPLE = 4


def rounded_alpha_mask(
    size: tuple[int, int],
    radius: int,
    *,
    supersample: int = ROUNDED_SUPERSAMPLE,
) -> Image.Image:
    """Anti-aliased grayscale (L) mask: white rounded rect, black outside.

    Draws at ``supersample``× then downscales with Lanczos, so the arc edges
    blend instead of showing stair-stepped corner pixels.
    """
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    ss = max(1, int(supersample))
    big = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, w * ss - 1, h * ss - 1],
        radius=max(0, int(radius)) * ss,
        fill=255,
    )
    return big.resize((w, h), Image.LANCZOS)


def rounded_layer(
    img: Image.Image,
    radius: int,
    *,
    supersample: int = ROUNDED_SUPERSAMPLE,
) -> Image.Image:
    """Return ``img`` as RGBA with anti-aliased rounded corners (transparent out)."""
    img = img.convert("RGBA")
    mask = rounded_alpha_mask(img.size, radius, supersample=supersample)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def drop_shadow(
    pane: Image.Image,
    *,
    offset: tuple[int, int] = (5, 6),
    blur: int = 24,
    spread: int = 2,
    opacity: float = 0.28,
) -> tuple[Image.Image, int]:
    dx, dy = int(offset[0]), int(offset[1])
    blur = max(0, int(blur))
    spread = max(0, int(spread))
    opacity = min(1.0, max(0.0, float(opacity)))
    pad = blur + spread + max(abs(dx), abs(dy)) + 2
    alpha = pane.convert("RGBA").getchannel("A")
    if spread:
        alpha = alpha.filter(ImageFilter.MaxFilter(2 * spread + 1))
    if opacity < 1.0:
        alpha = alpha.point(lambda v: int(v * opacity + 0.5))
    shape = Image.new("RGBA", alpha.size, (0, 0, 0, 255))
    shape.putalpha(alpha)
    layer = Image.new("RGBA", (pane.width + 2 * pad, pane.height + 2 * pad), (0, 0, 0, 0))
    layer.alpha_composite(shape, (pad + dx, pad + dy))
    if blur:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    return layer, pad
