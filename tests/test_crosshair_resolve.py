"""Shared crosshair system: one prosettings-first rule for every renderer."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

import crosshair_resolve  # noqa: E402


def test_unknown_nick_skips_prosettings():
    calls = []

    def fake_resolve(nick, fallback=None):
        calls.append(nick)
        return (fallback(), {"source": "demo"}) if fallback else ([], {"source": "none"})

    with patch.object(crosshair_resolve, "resolve_crosshair", side_effect=fake_resolve):
        with patch.object(crosshair_resolve, "demo_crosshair_cvars", return_value=["demo"]) as demo:
            cvars, info = crosshair_resolve.resolve_crosshair_cvars(
                "Unknown", "123", Path("x.dem"), csdm_cmd="csdm")

    assert cvars == ["demo"] and info["source"] == "demo"
    assert calls == [""]
    assert demo.call_args[0][:2] == ("123", Path("x.dem"))


def test_blank_nick_skips_prosettings():
    with patch.object(crosshair_resolve, "resolve_crosshair",
                       return_value=([], {"source": "none"})) as resolve:
        with patch.object(crosshair_resolve, "demo_crosshair_cvars", return_value=[]):
            cvars, info = crosshair_resolve.resolve_crosshair_cvars(
                "", "123", Path("x.dem"), csdm_cmd="csdm")

    assert cvars == [] and info["source"] == "none"
    assert resolve.call_args[0][0] == ""


def test_nick_goes_to_prosettings_first():
    with patch.object(
        crosshair_resolve, "resolve_crosshair",
        return_value=(["pro"], {"source": "prosettings"}),
    ) as resolve:
        with patch.object(crosshair_resolve, "demo_crosshair_cvars") as demo:
            cvars, info = crosshair_resolve.resolve_crosshair_cvars(
                "donk", "123", Path("x.dem"), csdm_cmd="csdm")

    assert cvars == ["pro"] and info["source"] == "prosettings"
    assert resolve.call_args[0][0] == "donk"
    assert demo.call_count == 0


def test_demo_lookup_decodes_share_code(tmp_path: Path):
    from crosshair_resolve import demo_crosshair_cvars

    payload = {"players": [
        {"steamId": "123", "crosshairShareCode": "CSGO-dGik3-tOynV-MWMqb-KMiLO-K7XXC"},
    ]}

    class _R:
        returncode = 0

    def fake_run(cmd, **kwargs):
        out = Path(cmd[cmd.index("--output-folder") + 1])
        (out / "a.json").write_text(json.dumps(payload), encoding="utf-8")
        return _R()

    with patch.object(crosshair_resolve.subprocess, "run", side_effect=fake_run):
        cvars = demo_crosshair_cvars("123", tmp_path / "x.dem", csdm_cmd="csdm")

    assert "cl_crosshairstyle 4" in cvars
    assert "cl_crosshaircolor 4" in cvars


def test_demo_lookup_miss_returns_empty(tmp_path: Path):
    from crosshair_resolve import demo_crosshair_cvars

    class _R:
        returncode = 0

    def fake_run(cmd, **kwargs):
        out = Path(cmd[cmd.index("--output-folder") + 1])
        (out / "a.json").write_text(json.dumps({"players": []}), encoding="utf-8")
        return _R()

    with patch.object(crosshair_resolve.subprocess, "run", side_effect=fake_run):
        assert demo_crosshair_cvars("999", tmp_path / "x.dem", csdm_cmd="csdm") == []


def test_shorts_wrapper_passes_nick():
    from shorts.render_shorts import _get_player_crosshair_cvars

    with patch("crosshair_resolve.resolve_crosshair_cvars",
               return_value=(["x"], {"source": "demo"})) as resolve:
        out = _get_player_crosshair_cvars("123", Path("x.dem"), "donk")

    assert out == ["x"]
    assert resolve.call_args[0][:3] == ("donk", "123", Path("x.dem"))


def test_highlights_wrapper_falls_back_to_registry_nick(tmp_path: Path, monkeypatch):
    import highlights.render_edit_timeline as ret

    accounts = [{"steam_id": "123", "nickname": "donk"}]
    (tmp_path / ".data").mkdir(exist_ok=True)
    (tmp_path / ".data" / "player_accounts.json").write_text(json.dumps(accounts), encoding="utf-8")
    monkeypatch.setattr(ret, "_PROJECT_ROOT", tmp_path)
    with patch("crosshair_resolve.resolve_crosshair_cvars",
               return_value=(["x"], {"source": "prosettings"})) as resolve:
        out = ret._get_player_crosshair_cvars("123", Path("x.dem"))

    assert out == ["x"]
    assert resolve.call_args[0][0] == "donk"


def test_hook_build_config_uses_shared_system(tmp_path: Path):
    from pov.render_hook import build_config

    demo = tmp_path / "x.dem"
    demo.write_bytes(b"0")
    plan = [{
        "pov_steam_id": "123", "pov_nick": "donk", "label": "4k",
        "tier": "4k", "round": 1,
        "windows": [{"start_tick": 100, "end_tick": 200}],
    }]
    with patch("crosshair_resolve.resolve_crosshair_cvars",
               return_value=(["cl_crosshairsize 1"], {"source": "prosettings"})) as resolve:
        cfg = build_config(plan, demo, tmp_path, 1280, 960, 60)

    assert resolve.call_args[0][:3] == ("donk", "123", demo)
    seq = cfg["sequences"][0]
    assert "cl_crosshairsize 1" in seq["cfg"]
    assert 'mirv_replace_name byXuid add x123 "donk"' in seq["cfg"]
