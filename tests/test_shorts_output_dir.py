"""Tests for Shorts output directory resolution."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts import resolve_output_dir

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _patch_renders_dir(monkeypatch, tmp_path: Path):
    # Make demo paths appear under tmp_path as a fake project root,
    # and point RENDERS_DIR to tmp_path/renders.
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    (fake_project / "renders").mkdir()
    abs_demo_root = fake_project / "demos"
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")


def test_hltv_with_player(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "spirit-vs-falcons"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "spirit-vs-falcons-m3-nuke.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo, player="76561198000000000")
    assert out == tmp_path / "renders" / "pov-spirit-vs-falcons-m3-nuke" / "shorts"
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
    assert out == tmp_path / "renders" / "hl-team-teses-vs-svnonethree" / "shorts"
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
    assert out.name == "shorts"
    assert "pov-match-slug-map" in out.parent.name


def test_hltv_player_ignored(monkeypatch, tmp_path):
    """Player is accepted but ignored — single per-match shorts folder."""
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "some-match"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "some-map.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out = resolve_output_dir(demo)  # no player needed
    assert out.name == "shorts"
    assert out.parent.name == "pov-some-map"


def test_unknown_path_raises(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "someother"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "match.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    with pytest.raises(ValueError, match="unknown demo path"):
        resolve_output_dir(demo, player="123")


def test_idempotent(monkeypatch, tmp_path):
    fake_project = tmp_path / "project"
    fake_project.mkdir()
    abs_demo_root = fake_project / "demos" / "hltv" / "m"
    abs_demo_root.mkdir(parents=True)
    demo = abs_demo_root / "m.dem"
    demo.write_text("")
    monkeypatch.setattr("shorts.RENDERS_DIR", tmp_path / "renders")

    out1 = resolve_output_dir(demo, player="1")
    out2 = resolve_output_dir(demo, player="1")
    assert out1 == out2
    assert out1.is_dir()