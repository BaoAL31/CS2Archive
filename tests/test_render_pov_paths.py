"""Tests for render output path resolution (HLAE requires absolute --output)."""

from __future__ import annotations

import sys
from pathlib import Path


import cs2archive.pov.render_pov as render_pov
from cs2archive.pov.render_pov import _sequence_cfg, resolve_output_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_resolve_output_dir_relative_becomes_absolute():
    out = resolve_output_dir("renders/test", "ignored.dem", "76561198000000000")
    assert out.is_absolute()
    assert out == (PROJECT_ROOT / "renders/test").resolve()


def test_resolve_output_dir_default_under_project():
    out = resolve_output_dir(None, "match-m1-nuke.dem", "76561198000000000")
    assert out.is_absolute()
    assert out == (render_pov._PROJECT_ROOT / "renders/pov-match-m1-nuke_76561198000000000").resolve()


def test_resolve_output_dir_preserves_absolute_input():
    absolute = Path("C:/renders/pov-test")
    out = resolve_output_dir(str(absolute), "x.dem", "1")
    assert out == absolute.resolve()


def test_sequence_cfg_inlines_rename_overrides():
    # mirv_replace_name lines must ride the per-round sequence cfg (exec'd
    # mid-demo with the hook loaded) — launch-time autoexec alone does not
    # apply them, and the HUD keeps the raw demo nicknames.
    text = _sequence_cfg(
        ["cl_crosshairdot 1"],
        {"76561198875633179": "dem0n"})
    assert "cl_crosshairdot 1" in text
    assert 'mirv_replace_name byXuid add x76561198875633179 "dem0n"' in text


def test_sequence_cfg_without_rename_has_no_replace_lines():
    text = _sequence_cfg(["cl_crosshairdot 1"], None)
    assert "cl_crosshairdot 1" in text
    assert "mirv_replace_name" not in text
