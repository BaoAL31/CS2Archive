"""scale_player crops transparent padding before sizing the subject."""
from __future__ import annotations

from PIL import Image

from thumbnail.generator import scale_player


def _padded_subject(canvas: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    img.paste(Image.new("RGBA", (x1 - x0, y1 - y0), (200, 40, 40, 255)), (x0, y0))
    return img


def test_scale_player_trims_left_padding():
    # 100x200 subject sitting 150px from the left of a 400x400 canvas.
    padded = _padded_subject((400, 400), (150, 100, 250, 300))
    tight = _padded_subject((100, 200), (0, 0, 100, 200))
    target = 400
    assert scale_player(padded, target).size == scale_player(tight, target).size
    assert scale_player(padded, target).size == (200, 400)


def test_scale_player_fully_opaque_is_unchanged_shape():
    solid = Image.new("RGBA", (400, 800), (200, 40, 40, 255))
    out = scale_player(solid, 400)
    assert out.size == (200, 400)
