"""Repeek-pane FACEIT intro: rounded pop-up panes, transparent middle."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from create_repeek_intro import (  # noqa: E402
    OUT_H,
    OUT_W,
    PANE_MARGIN,
    _pane_layout,
    compose_panes,
    match_id_from_backlog,
    pane_layers,
)
from intro_prepend import build_intro_filter  # noqa: E402


def _panes(w=400, h=900, lc=(200, 0, 0), rc=(0, 200, 0)):
    return Image.new("RGB", (w, h), lc), Image.new("RGB", (w, h), rc)


def test_compose_panes_frame_size_and_mode():
    out = compose_panes(*_panes())
    assert out.size == (OUT_W, OUT_H)
    assert out.mode == "RGBA"


def test_compose_panes_never_touch_frame_corners():
    out = compose_panes(*_panes()).convert("RGBA")
    for xy in ((0, 0), (OUT_W - 1, 0), (0, OUT_H - 1), (OUT_W - 1, OUT_H - 1)):
        assert out.getpixel(xy)[3] == 0, f"corner {xy} not clear"


def test_compose_panes_inset_left_and_right():
    out = compose_panes(*_panes()).convert("RGBA")
    left_margin = out.getpixel((PANE_MARGIN // 2, OUT_H // 2))
    right_margin = out.getpixel((OUT_W - PANE_MARGIN // 2, OUT_H // 2))
    assert left_margin[3] == 0
    assert right_margin[3] < 255
    assert out.getpixel((PANE_MARGIN + 30, OUT_H // 2))[3] == 255      # left pane body
    assert out.getpixel((OUT_W - PANE_MARGIN - 30, OUT_H // 2))[3] == 255


def test_compose_panes_middle_transparent():
    out = compose_panes(*_panes()).convert("RGBA")
    assert out.getpixel((OUT_W // 2, OUT_H // 2))[3] == 0


def test_compose_panes_corners_are_rounded():
    out = compose_panes(*_panes()).convert("RGBA")
    # The pane's own top-left corner pixel is not fully opaque (rounded).
    assert out.getpixel((PANE_MARGIN, PANE_MARGIN))[3] < 255
    # ...while a point just inside the pane body is.
    assert out.getpixel((PANE_MARGIN + 40, OUT_H // 2))[3] == 255


def test_pane_layers_split_left_and_right():
    panes = _panes()
    left_layer, right_layer, left_rect, right_rect = pane_layers(*panes, OUT_W, OUT_H)

    assert left_layer.size == right_layer.size == (OUT_W, OUT_H)
    assert left_layer.mode == right_layer.mode == "RGBA"
    assert left_rect[2] > 0 and left_rect[3] > 0
    assert right_rect[2] > 0 and right_rect[3] > 0
    assert right_rect[0] > left_rect[0] + left_rect[2]
    assert left_layer.getpixel((left_rect[0] + 30, OUT_H // 2))[3] == 255
    assert right_layer.getpixel((OUT_W - 86, OUT_H // 2))[3] == 255
    assert left_layer.getpixel((OUT_W - 86, OUT_H // 2))[3] == 0
    assert right_layer.getpixel((left_rect[0] + 30, OUT_H // 2))[3] == 0


def test_intro_filter_slides_panes_with_ease_in_out():
    fc = build_intro_filter(
        (2560, 1440, 60.0), 5.0, 0.5, 0.5,
        [56, 56, 700, 1300], [1804, 56, 700, 1300],
    )

    assert fc.count("overlay=x='") == 2
    assert "pow(min(max(t/0.5,0),1),2)" in fc
    assert "fade" not in fc
    assert "if(lt(t,4.5),0," in fc
    assert str(-(56 + 700)) in fc
    assert str(2560 - 1804) in fc


def test_compose_panes_shadow_falls_bottom_right():
    panes = _panes()
    pane, _, (pane_x, pane_y), _ = _pane_layout(*panes, OUT_W, OUT_H)
    out = compose_panes(*panes).convert("RGBA")
    right = out.getpixel((pane_x + pane.width + 8, OUT_H // 2))
    bottom = out.getpixel((pane_x + pane.width // 2, pane_y + pane.height + 8))
    left = out.getpixel((pane_x - 8, OUT_H // 2))
    top = out.getpixel((pane_x + pane.width // 2, pane_y - 8))

    assert right[:3] == bottom[:3] == left[:3] == top[:3] == (0, 0, 0)
    assert right[3] > left[3] * 3
    assert bottom[3] > top[3] * 3
    assert max(right[3], bottom[3]) > 0


def test_compose_panes_never_overlap_shrinks_instead():
    left, right = _panes(w=2000, h=1000, lc=(10, 10, 10), rc=(20, 20, 20))
    out = compose_panes(left, right).convert("RGBA")
    assert out.size == (OUT_W, OUT_H)
    # Wide panes shrink to meet -> the middle is covered, not overlapped.
    assert out.getpixel((OUT_W // 2, OUT_H // 2))[3] == 255


def test_match_id_from_backlog(tmp_path: Path):
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"faceit_match_id": "1-abc-def"}), encoding="utf-8")
    assert match_id_from_backlog(card) == "1-abc-def"


def test_match_id_from_backlog_requires_id(tmp_path: Path):
    card = tmp_path / "card.json"
    card.write_text(json.dumps({"player": "donk"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        match_id_from_backlog(card)
