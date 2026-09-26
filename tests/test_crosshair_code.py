"""Share-code decoding: presets → RGB; Rush Hour pixel cvars."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from crosshair_code import (  # noqa: E402
    crosshair_to_convars,
    decode_crosshair,
    encode_crosshair,
    old_scale_to_pixels,
)


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


def test_donk_old_scale_to_1440_pixels():
    # Length 1 / Thickness 1.5 / Gap -4 at 2560x1440 → 3 / 5 / 3
    assert old_scale_to_pixels(1.0, 1.5, -4.0, screen_height=1440) == (3, 5, 3)
    assert old_scale_to_pixels(1.0, 1.5, -4.0, screen_height=1080) == (2, 3, 2)


def test_donk_prosettings_code_is_mint_custom_pixels():
    cvars = crosshair_to_convars(
        decode_crosshair("CSGO-VeUo2-qw76k-xJeKX-izb9a-GVOAK"),
        screen_height=1440,
    )

    assert "cl_crosshairstyle 4" in cvars
    assert "cl_crosshair_length 3" in cvars
    assert "cl_crosshair_thickness 5" in cvars
    assert "cl_crosshair_gap 3" in cvars
    assert "cl_crosshaircolor_r 0" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 165" in cvars
    assert "cl_crosshaircolor_a 255" in cvars
    assert not any(c.startswith("cl_crosshairsize ") for c in cvars)
    assert not any(c.startswith("cl_crosshairusealpha ") for c in cvars)


def test_demo_code_is_white_custom():
    cvars = crosshair_to_convars(decode_crosshair("CSGO-dGik3-tOynV-MWMqb-KMiLO-K7XXC"))

    assert "cl_crosshaircolor_r 255" in cvars
    assert "cl_crosshaircolor_g 255" in cvars
    assert "cl_crosshaircolor_b 255" in cvars


def test_presets_expand_to_rgb():
    for preset, rgb in (
        (0, (250, 50, 50)),
        (1, (50, 250, 50)),
        (2, (250, 250, 50)),
        (3, (50, 50, 250)),
        (4, (50, 250, 250)),
    ):
        cvars = crosshair_to_convars(_base(color=preset))
        assert f"cl_crosshaircolor_r {rgb[0]}" in cvars
        assert f"cl_crosshaircolor_g {rgb[1]}" in cvars
        assert f"cl_crosshaircolor_b {rgb[2]}" in cvars


def test_unknown_nibbles_fall_back_to_custom():
    for nibble in (6, 7):
        cvars = crosshair_to_convars(_base(color=nibble, red=10, green=20, blue=30))
        assert "cl_crosshaircolor_r 10" in cvars
        assert "cl_crosshaircolor_g 20" in cvars
        assert "cl_crosshaircolor_b 30" in cvars


def test_already_pixel_values_pass_through():
    cvars = crosshair_to_convars(_base(length=8, thickness=2, gap=4))
    assert "cl_crosshair_length 8" in cvars
    assert "cl_crosshair_thickness 2" in cvars
    assert "cl_crosshair_gap 4" in cvars


def test_effective_height_clamps_to_desktop(monkeypatch):
    import crosshair_code as cc

    monkeypatch.setattr(cc, "effective_crosshair_height", lambda r: min(r, 1080))
    assert cc.effective_crosshair_height(1440) == 1080
    assert old_scale_to_pixels(1.0, 1.5, -4.0, screen_height=1080) == (2, 3, 2)


def test_encode_round_trips_real_codes():
    for code in (
        "CSGO-VeUo2-qw76k-xJeKX-izb9a-GVOAK",
        "CSGO-dGik3-tOynV-MWMqb-KMiLO-K7XXC",
    ):
        assert encode_crosshair(decode_crosshair(code)) == code


def test_encode_version_byte_changes_code_but_stays_valid():
    settings = decode_crosshair("CSGO-VeUo2-qw76k-xJeKX-izb9a-GVOAK")
    bumped = encode_crosshair(settings, version=2)
    assert bumped != "CSGO-VeUo2-qw76k-xJeKX-izb9a-GVOAK"
    assert decode_crosshair(bumped)["length"] == settings["length"]
