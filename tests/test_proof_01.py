"""Style-01 HTML must keep the study #proof layout numbers."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from thumbnail.proof_01 import WIDTH, HEIGHT, build_html, render

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "assets" / "fonts" / "Montserrat-Bold.ttf"


def test_html_matches_proof_css():
    html = build_html(
        bg=FONT,
        main_avatar=FONT,
        name="donk",
        score="23-14",
        sub="DUO w/ MAGIXX",
        costar_left=FONT,
        costar_right=FONT,
    )
    assert "width: 1280px" in html
    assert "height: 720px" in html
    assert "font-size: 84px" in html
    assert "font-size: 142px" in html
    assert "width: 480px; height: 640px; left: 130px; bottom: -22px" in html
    assert "width: 300px; height: 400px; left: -20px; bottom: -14px" in html
    assert "width: 300px; height: 400px; left: 520px; bottom: -14px" in html
    assert "#efc34f" in html
    assert "display: none;" not in html.split(".portrait.left")[1].split("}")[0]
    assert "display: none;" not in html.split(".portrait.right")[1].split("}")[0]


def test_single_costar_uses_study_duo_boxes():
    html = build_html(
        bg=FONT,
        main_avatar=FONT,
        name="apEX",
        score="15-14",
        sub="w/ jL",
        costar_right=FONT,
    )
    assert "width: 560px; height: 700px; left: -20px; bottom: -20px" in html
    assert "width: 360px; height: 450px; left: 360px; bottom: -16px" in html
    assert "left: 520px" not in html.split(".portrait.right")[1].split("}")[0]


def test_hides_costars_and_sub_when_omitted():
    html = build_html(
        bg=FONT,
        main_avatar=FONT,
        name="HeavyGod",
        score="15-7",
    )
    assert "display: none;" in html.split(".portrait.left")[1].split("}")[0]
    assert "display: none;" in html.split(".portrait.right")[1].split("}")[0]
    assert "display: none;" in html.split(".proof-copy .sub")[1].split("}")[0]


def test_render_png_size():
    pytest.importorskip("playwright")
    if not FONT.is_file():
        pytest.skip("Montserrat missing")
    dest = ROOT / "tmp" / "proof_01_test.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    render(
        bg=FONT,
        main_avatar=FONT,
        name="donk",
        score="23-14",
        dest=dest,
        sub="DUO w/ MAGIXX",
    )
    im = Image.open(dest)
    try:
        assert im.size == (WIDTH, HEIGHT)
    finally:
        im.close()
    dest.unlink(missing_ok=True)
