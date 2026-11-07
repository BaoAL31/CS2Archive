"""Tests for render output path resolution (HLAE requires absolute --output)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


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


def test_sequence_cfg_carries_screenmessage_display_kill():
    # Round-1 segments print server/chat lines during the CSDM seek; the
    # client display timer (not the cheat-locked show cvar, not the
    # server-side bare timer) is what hides them.
    text = _sequence_cfg(["cl_crosshairdot 1"], None)
    assert "cl_screenmessage_notifytime 0" in text


def test_resolve_player_nick_prefers_cli_flag(monkeypatch):
    monkeypatch.setattr(
        "cs2archive.crosshair_resolve.canonical_nick",
        lambda *a, **k: "NiKo")
    from cs2archive.pov.render_pov import _resolve_player_nick

    assert _resolve_player_nick("donk", "1") == "donk"


def test_resolve_player_nick_falls_back_to_canonical(monkeypatch):
    # Manual runs omit --player: the blank nick must resolve via steam_id or
    # the render silently ships the game-default crosshair (NiKo Mirage).
    monkeypatch.setattr(
        "cs2archive.crosshair_resolve.canonical_nick",
        lambda sid, fallback="": "NiKo" if sid == "76561198041683378" else "")
    from cs2archive.pov.render_pov import _resolve_player_nick

    assert _resolve_player_nick("", "76561198041683378") == "NiKo"
    assert _resolve_player_nick("", "0") == ""


def test_pro_crosshair_gate_aborts_for_recognised(monkeypatch):
    # A recognised pro with no resolved crosshair must fail loudly, never
    # burn hours on the game-default crosshair. Unknown players keep the
    # old warn-and-proceed behaviour.
    monkeypatch.setattr(
        "cs2archive.crosshair_resolve.canonical_nick",
        lambda sid, fallback="": "NiKo" if sid == "1" else "")
    from cs2archive.pov.render_pov import _ensure_pro_crosshair

    with pytest.raises(SystemExit):
        _ensure_pro_crosshair([], "1", "NiKo")
    _ensure_pro_crosshair([], "2", "")  # unknown: warns only
    _ensure_pro_crosshair(["cl_crosshair_length 2"], "1", "NiKo")  # ok
