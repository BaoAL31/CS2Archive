"""Fail-loud Repeek roster capture: layout/PNG gates must raise, never continue."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scrapers.repeek_snapshot import (  # noqa: E402
    RepeekCaptureError,
    assert_column_pngs,
    assert_roster_layout,
    wait_repeek_ready,
    _clip_for,
)


def _card(x: float, y: float, w: float = 392.0, h: float = 137.0) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def _good_boxes() -> list[dict]:
    ys = [523.6, 668.7, 813.7, 958.8, 1103.8]
    boxes = []
    for y in ys:
        boxes.append(_card(344.0, y))
        boxes.append(_card(1184.0, y))
    return boxes


def test_known_good_5v5_layout_passes():
    boxes = _good_boxes()
    cols = assert_roster_layout(boxes)
    assert [c["side"] for c in cols] == ["left", "right"]
    assert abs(cols[0]["h"] - cols[1]["h"]) < 40


def test_nine_cards_fails():
    boxes = _good_boxes()[:-1]
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_CARD_COUNT"


def test_eleven_cards_fails():
    boxes = _good_boxes() + [_card(344.0, 1250.0)]
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_CARD_COUNT"


def test_uneven_sides_fails():
    boxes = _good_boxes()
    boxes[-1] = _card(344.0, 1103.8)  # sixth on the left
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_SIDE_COUNT"


def test_card_taller_than_expected_fails():
    boxes = _good_boxes()
    boxes[0] = _card(344.0, 523.6, h=220.0)
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_CARD_SIZE"


def test_columns_too_close_fails():
    boxes = []
    ys = [523.6, 668.7, 813.7, 958.8, 1103.8]
    for y in ys:
        boxes.append(_card(344.0, y))
        boxes.append(_card(500.0, y))
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_COLUMN_GAP"


def test_missing_row_alignment_fails():
    boxes = _good_boxes()
    boxes[1] = _card(1184.0, 540.0)  # right of row 0 shifted
    with pytest.raises(RepeekCaptureError) as ei:
        assert_roster_layout(boxes)
    assert ei.value.code == "REPEEK_ROW_ALIGN"


def test_blank_column_png_fails(tmp_path: Path):
    left = tmp_path / "repeek_left.png"
    right = tmp_path / "repeek_right.png"
    Image.new("RGB", (458, 767), (12, 12, 12)).save(left)
    Image.new("RGB", (458, 767), (12, 12, 12)).save(right)
    with pytest.raises(RepeekCaptureError) as ei:
        assert_column_pngs(left, right)
    assert ei.value.code == "REPEEK_PNG_BLANK"


def test_tiny_column_png_fails(tmp_path: Path):
    left = tmp_path / "repeek_left.png"
    right = tmp_path / "repeek_right.png"
    Image.new("RGB", (40, 40), (80, 40, 20)).save(left)
    Image.new("RGB", (40, 40), (20, 80, 40)).save(right)
    with pytest.raises(RepeekCaptureError) as ei:
        assert_column_pngs(left, right)
    assert ei.value.code == "REPEEK_PNG_SIZE"


def test_missing_column_png_fails(tmp_path: Path):
    left = tmp_path / "repeek_left.png"
    right = tmp_path / "repeek_right.png"
    im = Image.new("RGB", (458, 767), (20, 20, 20))
    from PIL import ImageDraw
    ImageDraw.Draw(im).rectangle([20, 20, 200, 400], fill=(200, 80, 40))
    im.save(left)
    with pytest.raises(RepeekCaptureError) as ei:
        assert_column_pngs(left, right)
    assert ei.value.code == "REPEEK_PNG_MISSING"


def test_known_good_column_pngs_pass():
    d = ROOT / "renders/stat-strips/1-75510475-266d-4a7a-a384-72ed968f005d"
    left, right = d / "repeek_left.png", d / "repeek_right.png"
    if not left.is_file() or not right.is_file():
        pytest.skip("no captured roster pngs on disk")
    assert_column_pngs(left, right)


def test_clip_that_does_not_fit_viewport_fails():
    with pytest.raises(RepeekCaptureError) as ei:
        _clip_for(
            {"x": 0, "y": 500, "w": 458, "h": 767},
            {"width": 1920, "height": 1080},
            pad_l=0, pad_t=0, pad_r=0, pad_b=0,
        )
    assert ei.value.code == "REPEEK_CLIP_CLIPPED"


def test_wait_timeout_does_not_continue(monkeypatch: pytest.MonkeyPatch):
    import scrapers.repeek_snapshot as rs

    monkeypatch.setattr(rs, "repeek_widget_status", lambda _p: {"widgets": 2, "ready": 0})
    monkeypatch.setattr(rs, "_scroll_roster", lambda _p: None)

    class _Page:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    with pytest.raises(RepeekCaptureError) as ei:
        wait_repeek_ready(_Page(), timeout_ms=20)
    assert ei.value.code == "REPEEK_NOT_READY"
