"""PiP dressing: drop shadow sprite (no white outline) + ease-in-out slide."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from imgutil import drop_shadow, rounded_layer  # noqa: E402
from overlay._common import (  # noqa: E402
    PIP_SHADOW_BLUR,
    PIP_SHADOW_OFFSET,
    PIP_SHADOW_OPACITY,
    _pip_body,
    pip_shadow_pad,
)
from overlay.overlay_pov import _build_pip_overlay, _make_pip_shadow  # noqa: E402
from overlay.overlay_utilcams import PipClip  # noqa: E402


def _clip(**overrides):
    args = {
        "clip_path": Path("flight_smoke.mp4"),
        "start_frame": 100,
        "end_frame": 280,
        "util_type": "smoke",
    }
    args.update(overrides)
    return PipClip(**args)


def test_shadow_pad_matches_drop_shadow():
    pane = rounded_layer(Image.new("RGBA", (120, 80), (255, 255, 255, 255)), 12)
    _, pad = drop_shadow(
        pane,
        offset=PIP_SHADOW_OFFSET,
        blur=PIP_SHADOW_BLUR,
        spread=0,
        opacity=PIP_SHADOW_OPACITY,
    )
    assert pad == pip_shadow_pad()


def test_make_pip_shadow_sprite(tmp_path: Path):
    body = 200
    out = tmp_path / "pip_shadow_200.png"
    pad = _make_pip_shadow(out, body, 16)

    assert pad == pip_shadow_pad()
    shadow = Image.open(out).convert("RGBA")
    assert shadow.size == (body + 2 * pad, body + 2 * pad)
    pixels = shadow.load()
    assert pixels[pad + body - 4, pad + body // 2][:3] == (0, 0, 0)
    assert pixels[pad + body - 4, pad + body // 2][3] > pixels[pad + 4, pad + body // 2][3]


def test_pip_overlay_has_shadow_and_slide_not_outline():
    fc, tag = _build_pip_overlay(
        _clip(), "[0:v]", 1, 2560, 1440, 60.0,
        inner_mask_idx=2, shadow_idx=3,
    )
    body = _pip_body(1440)
    pad = pip_shadow_pad()
    slide_from = -(12 + body + pad)

    assert tag == "pip0_1"
    assert "lutrgb" not in fc
    assert "white@1" not in fc
    assert "[3:v]" in fc
    assert "between(n\\,100\\,280)" in fc
    assert "pow(min(max((n-100)/18,0),1),2)" in fc
    assert "(n-262)/18" in fc
    assert f"x='12+({slide_from}*(" in fc
    assert f"x='{12 - pad}+({slide_from}*(" in fc
    assert fc.count("overlay=x='") == 2


def test_pip_overlay_without_shadow_is_single_static_overlay():
    fc, _ = _build_pip_overlay(
        _clip(), "[0:v]", 1, 2560, 1440, 60.0,
        inner_mask_idx=2, shadow_idx=None,
    )

    assert "[3:v]" not in fc
    assert fc.count("overlay=x='") == 1
    assert "overlay=x='12+" in fc
