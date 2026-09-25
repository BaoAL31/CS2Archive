"""Share-code decoding: presets pass through, custom carries exact RGB."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from crosshair_code import crosshair_to_convars, decode_crosshair  # noqa: E402


def _base(**over):
    ch = {
        "style": 4, "length": 1.0, "thickness": 1.0, "gap": -4.0,
        "outlineEnabled": False, "outline": 0.0, "centerDotEnabled": False,
        "color": 1, "red": 0, "green": 0, "blue": 0, "alpha": 255,
        "followRecoil": False, "tStyleEnabled": False,
        "deployedWeaponGapEnabled": False, "alphaEnabled": True,
        "splitDistance": 3, "fixedCrosshairGap": 0.0, "innerSplitAlpha": 0.0,
        "outerSplitAlpha": 1.0, "splitSizeRatio": 1.0, "sniper_width": 1,
    }
    ch.update(over)
    return ch


def test_donk_prosettings_code_is_mint_custom():
    cvars = crosshair_to_convars(decode_crosshair("CSGO-VeUo2-qw76k-xJeKX-izb9a-GVOAK"))

    assert "cl_crosshairstyle 4" in cvars
    assert "cl_crosshairthickness 1.5" in cvars
    assert "cl_crosshairgap -4.0" in cvars
    assert "cl_crosshaircolor 5" in cvars
    assert "cl_crosshaircolor_r 0" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 165" in cvars
    assert "cl_crosshairusealpha 1" in cvars


def test_demo_code_is_white_custom():
    cvars = crosshair_to_convars(decode_crosshair("CSGO-dGik3-tOynV-MWMqb-KMiLO-K7XXC"))

    assert "cl_crosshaircolor 5" in cvars
    assert "cl_crosshaircolor_r 255" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 255" in cvars


def test_presets_pass_through_without_rgb():
    for preset in (0, 1, 2, 3, 4):
        cvars = crosshair_to_convars(_base(color=preset))
        assert f"cl_crosshaircolor {preset}" in cvars
        assert not [c for c in cvars if c.startswith("cl_crosshaircolor_")]


def test_unknown_nibbles_fall_back_to_custom():
    for nibble in (6, 7):
        cvars = crosshair_to_convars(_base(color=nibble, red=10, green=20, blue=30))
        assert "cl_crosshaircolor 5" in cvars
        assert "cl_crosshaircolor_r 10" in cvars
