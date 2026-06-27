"""Tests for render output path resolution (HLAE requires absolute --output)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import render_pov
from render_pov import resolve_output_dir

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
