"""Tests for Shorts output directory resolution."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts import resolve_output_dir


def test_hltv_with_player(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "spirit-vs-falcons"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "spirit-vs-falcons-m3-nuke.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo, player="76561198000000000")
    assert out == tmp_path / "renders" / "hl-spirit-vs-falcons-m3-nuke"
    assert out.is_dir()


def test_faceit_no_player_needed(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "faceit"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "team-teses-vs-svnonethree.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo)
    assert out == tmp_path / "renders" / "hl-team-teses-vs-svnonethree"
    assert out.is_dir()


def test_hltv_absolute_path(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "match-slug"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "match-slug-map.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo, player="123456789")
    assert "hl-match-slug-map" in str(out)
    assert out.is_dir()


def test_hltv_player_ignored(monkeypatch, tmp_path):
    """Player is accepted but ignored — single per-match hl- folder."""
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "some-match"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "some-map.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo)
    assert out.name == "hl-some-map"


def test_hltv_faceit_same_prefix(monkeypatch, tmp_path):
    """Both HLTV and FACEIT use hl- prefix now."""
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    hltv_demo = tmp_path / "demos" / "hltv" / "m" / "m.dem"
    hltv_demo.parent.mkdir(parents=True)
    hltv_demo.write_text("")
    faceit_demo = tmp_path / "demos" / "faceit" / "f.dem"
    faceit_demo.parent.mkdir(parents=True)
    faceit_demo.write_text("")

    hltv_out = resolve_output_dir(hltv_demo)
    faceit_out = resolve_output_dir(faceit_demo)
    assert hltv_out.name.startswith("hl-")
    assert faceit_out.name.startswith("hl-")


def test_unknown_path_raises(monkeypatch, tmp_path):
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")
    demo = tmp_path / "demos" / "someother" / "match.dem"
    demo.parent.mkdir(parents=True)
    demo.write_text("")

    with pytest.raises(ValueError, match="unknown demo path"):
        resolve_output_dir(demo, player="123")


def test_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")
    demo = tmp_path / "demos" / "hltv" / "m" / "m.dem"
    demo.parent.mkdir(parents=True)
    demo.write_text("")

    out1 = resolve_output_dir(demo)
    out2 = resolve_output_dir(demo)
    assert out1 == out2
    assert out1.is_dir()