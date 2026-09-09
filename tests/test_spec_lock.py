"""Tests for the POV spec lock (deathcam stays on POV player post-death)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

import render_pov


def test_spec_lock_snippet_shipped() -> None:
    src = Path(render_pov._PROJECT_ROOT) / "assets" / render_pov.SPEC_LOCK_SNIPPET
    assert src.is_file(), "snippet missing from assets/"
    text = src.read_text(encoding="utf-8")
    assert "clientFrameStageNotify" in text  # every-frame re-issue
    assert "spec_player" in text
    assert "getSanitizedPlayerName" in text  # name-based, not index


def test_autoexec_lock_lines(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", tmp_path / "autoexec_render.cfg")
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", tmp_path / "render_crosshair.cfg")
    render_pov._write_render_autoexec(["crosshair 1"], None, "NiKo")
    text = (tmp_path / "autoexec_render.cfg").read_text(encoding="utf-8")
    assert 'mirv_script_load "mirv_script_spec_lock_name.js"' in text
    assert 'mirv_script_spec_lock_name "NiKo"' in text
    assert "spec_mode 1" in text


def test_autoexec_no_lock_without_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", tmp_path / "autoexec_render.cfg")
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", tmp_path / "render_crosshair.cfg")
    render_pov._write_render_autoexec(["crosshair 1"], None, None)
    text = (tmp_path / "autoexec_render.cfg").read_text(encoding="utf-8")
    assert "mirv_script_spec_lock" not in text


def test_autoexec_lock_name_quotes_stripped(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", tmp_path / "autoexec_render.cfg")
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", tmp_path / "render_crosshair.cfg")
    render_pov._write_render_autoexec([], None, 'Ni"Ko')
    text = (tmp_path / "autoexec_render.cfg").read_text(encoding="utf-8")
    assert 'mirv_script_spec_lock_name "NiKo"' in text


def test_spec_lock_cfg_writer(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(render_pov, "GAME_CFG", tmp_path)
    render_pov._write_spec_lock_cfg("NiKo")
    text = (tmp_path / "spec_lock.cfg").read_text(encoding="utf-8")
    assert 'mirv_script_load "mirv_script_spec_lock_name.js"' in text
    assert 'mirv_script_spec_lock_name "NiKo"' in text
    assert "spec_mode 1" in text
    render_pov._write_spec_lock_cfg(None)
    assert "off" in (tmp_path / "spec_lock.cfg").read_text(encoding="utf-8")


def test_pov_cfg_chains_spec_lock() -> None:
    text = (Path(render_pov._PROJECT_ROOT) / "assets" / "cs2_pov.cfg").read_text(encoding="utf-8")
    assert text.rstrip().splitlines()[-1].strip() == "exec spec_lock"


if __name__ == "__main__":
    test_spec_lock_snippet_shipped()
    print("PASS (fixture tests run under pytest)")
