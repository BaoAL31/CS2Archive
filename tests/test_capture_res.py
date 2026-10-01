"""Capture-resolution policy (CR-17).

The pro's own in-game resolution is ignored: every POV renders at the tallest
resolution this machine supports **at the POV's aspect ratio**, and
``concat_rounds`` stretches that native frame to the 16:9 export. Measured
2026-10 on the 1920x1080 rig: requesting 1920x1440 for a 4:3 POV produced a
1920x1080 clip — HLAE clamped the height but kept the width, silently
switching the frame to 16:9. Hence the desktop-height ceiling.

Golden values below are the numbers the machine actually renders (verified with
a 1-round probe render: 1440x1080 clip, DAR 4:3).
"""
from __future__ import annotations

import pytest

from cs2archive import capture_res


@pytest.fixture(autouse=True)
def _pin_desktop(monkeypatch: pytest.MonkeyPatch):
    """Pin the desktop so the goldens do not depend on the test machine."""
    monkeypatch.setattr(capture_res, "desktop_height", lambda: 1080)
    monkeypatch.setattr(capture_res, "desktop_width", lambda: 1920)


@pytest.mark.parametrize(
    ("aspect", "expected"),
    [
        ("4:3", (1440, 1080)),
        ("16:9", (1920, 1080)),
        ("16:10", (1728, 1080)),
        ("5:4", (1350, 1080)),
        # Wider than the desktop: width is the binding bound, ratio preserved.
        ("21:9", (1920, 822)),
    ],
)
def test_machine_max_per_aspect(aspect: str, expected: tuple[int, int]):
    assert capture_res.capture_size_for_aspect(aspect) == expected


def test_prosettings_resolution_is_ignored_only_its_aspect_survives():
    """TeSeS's 1024x768 must not become the capture size."""
    acct = {"resolution": "1024x768", "aspect_ratio": "4:3",
            "capture_width": 1024, "capture_height": 768}
    assert capture_res.capture_size_for_player(acct) == (1440, 1080)


def test_aspect_parses_from_resolution_string_when_ratio_missing():
    acct = {"resolution": "1280x960"}
    assert capture_res.capture_size_for_player(acct) == (1440, 1080)


def test_unknown_player_returns_none_then_caller_falls_back_to_16_9():
    assert capture_res.capture_size_for_player(None) is None
    assert capture_res.capture_size_for_player({}) is None
    assert capture_res.capture_size_for_aspect("") == (1920, 1080)


def test_height_never_exceeds_desktop():
    """Above the desktop HLAE clamps height and keeps width -> aspect changes."""
    w, h = capture_res.capture_size_for_aspect("4:3", max_height=1440)
    assert (w, h) == (1440, 1080)


def test_dimensions_are_even():
    for aspect in ("4:3", "16:9", "16:10", "5:4", "21:9", "32:9"):
        w, h = capture_res.capture_size_for_aspect(aspect)
        assert w % 2 == 0 and h % 2 == 0, aspect
