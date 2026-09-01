"""HLTV overlay thumbs draw the overlay badge when variant=overlay."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from thumbnail.layouts import generate


def _blank_bg(tmp_path: Path) -> tuple[Path, Path]:
    bg = tmp_path / "bg.jpg"
    Image.new("RGB", (1280, 720), (30, 30, 30)).save(bg, "JPEG")
    avatar = tmp_path / "av.png"
    Image.new("RGBA", (400, 800), (200, 40, 40, 255)).save(avatar)
    return bg, avatar


def test_overlay_variant_differs_from_raw(tmp_path: Path):
    bg, avatar = _blank_bg(tmp_path)
    kwargs = dict(
        bg_path=bg,
        avatar_path=avatar,
        player_name="ropz",
        kd="20-10",
        rating="1.20",
        map_name="Nuke",
        match_detail="FaZe vs Vitality",
    )
    raw = generate(**kwargs, variant="raw")
    overlay = generate(**kwargs, variant="overlay")
    assert overlay.size == raw.size
    assert list(overlay.getdata()) != list(raw.getdata())


def test_overlay_badge_paints_top_left(tmp_path: Path):
    bg, avatar = _blank_bg(tmp_path)
    img = generate(
        bg_path=bg,
        avatar_path=avatar,
        player_name="ropz",
        kd="20-10",
        rating="1.20",
        map_name="Nuke",
        match_detail="FaZe vs Vitality",
        variant="overlay",
    )
    # Badge is top-left; a raw grey background would stay near (30,30,30).
    px = img.getpixel((40, 40))
    assert px != (30, 30, 30)
