"""HUD names must never carry markup: Swift rows render text literally."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from overlay.swift_demoui import _strip_markup, patch_runtime  # noqa: E402

_EVIL = "<span class='decorated-player-name__control mount'>blyka</span>"


def test_strip_markup_removes_tags():
    assert _strip_markup(_EVIL) == "blyka"
    assert _strip_markup("<span class='x'") == ""
    assert _strip_markup("donk") == "donk"
    assert _strip_markup("  a  b ") == "a  b"


def test_patch_runtime_strips_names_and_cleans_display():
    src = (
        "if (name) name.text = player.name;\n"
        "\tfunction _RenderSpeakingPlayers(slots) {\n"
        "function _RunMaskCommands(low, high, status) {\n"
        "function _UnmuteNativePlayer(player) {\n"
        "function _FilterSpeakingSlotsForSelection(slots) {\n"
        "function _SetMenuVisible(visible) {\n"
    )
    out = patch_runtime(
        src, ["76561199646115626"], {"76561199646115626": _EVIL}, native=True,
    )

    assert "_CleanPlayerName(" in out
    assert "_EVIL" not in out
    assert _EVIL not in out
    assert "blyka" in out


def test_render_autoexec_sanitizes_rename_values(tmp_path: Path, monkeypatch):
    import render_pov

    monkeypatch.setattr(render_pov, "GAME_CFG", tmp_path)
    monkeypatch.setattr(render_pov, "AUTOEXEC_RENDER", tmp_path / "autoexec_render.cfg")
    monkeypatch.setattr(render_pov, "RENDER_CROSSHAIR_CFG", tmp_path / "render_crosshair.cfg")
    render_pov._write_render_autoexec(
        ["cl_crosshairsize 1"], {"76561199646115626": _EVIL}, None,
    )

    text = (tmp_path / "autoexec_render.cfg").read_text(encoding="utf-8")
    assert _EVIL not in text
    assert 'mirv_replace_name byXuid add x76561199646115626 "blyka"' in text


def test_merge_voice_names_prefers_canonical_keeps_demo():
    from render_pov import _merge_voice_names

    out = _merge_voice_names(
        {"76561199032006224": "kyousuke"},
        {"76561199032006224": "Ikuvi", "76561199646115626": "blyka", "x": ""},
    )

    assert out == {"76561199032006224": "kyousuke", "76561199646115626": "blyka"}


def test_demo_player_names_uses_recorded_names(tmp_path: Path, monkeypatch):
    import render_pov

    class _Info:
        def iterrows(self):
            yield 0, {"steamid": "76561199646115626", "name": "blyka"}
            yield 1, {"steamid": "", "name": "ghost"}

    class _DP:
        def __init__(self, *a, **k):
            pass

        def parse_player_info(self):
            return _Info()

    monkeypatch.setitem(sys.modules, "demoparser2", type("M", (), {"DemoParser": _DP})())
    render_pov._DEMO_NAMES_CACHE.clear()
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"0")

    assert render_pov._demo_player_names(str(demo)) == {"76561199646115626": "blyka"}


def test_faceit_rename_map_sanitizes_names(tmp_path: Path, monkeypatch):
    import pipeline

    class _Info:
        def iterrows(self):
            yield 0, {"steamid": "76561199646115626"}

    class _DP:
        def __init__(self, *a, **k):
            pass

        def parse_player_info(self):
            return _Info()

    monkeypatch.setitem(sys.modules, "demoparser2", type("M", (), {"DemoParser": _DP})())
    import types
    faceit_pkg = types.ModuleType("scripts.faceit")
    faceit_pkg.__path__ = [str(ROOT / "scripts" / "faceit")]
    faceit_names = types.ModuleType("scripts.faceit.faceit_names")
    faceit_names.known_pro_steam_ids = lambda: {"76561199646115626": _EVIL}
    monkeypatch.setitem(sys.modules, "scripts.faceit", faceit_pkg)
    monkeypatch.setitem(sys.modules, "scripts.faceit.faceit_names", faceit_names)
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"0")

    assert pipeline._faceit_rename_map(demo) == {"76561199646115626": "blyka"}


def test_cached_rename_map_parses_once(tmp_path: Path, monkeypatch):
    import types

    import pipeline

    calls = []
    monkeypatch.setattr(
        pipeline, "_faceit_rename_map", lambda p: calls.append(p) or {"1": "x"}
    )
    stub = types.SimpleNamespace(_rename_map=None, demo_path=tmp_path / "match.dem")
    cached = pipeline.Pipeline._cached_rename_map.__get__(stub)
    assert cached() == {"1": "x"}
    assert cached() == {"1": "x"}
    assert len(calls) == 1
