"""Steam-online HLAE preflight helpers (tmp dirs only)."""

from __future__ import annotations

import json

from hook_aware import (
    block_steam_overlay,
    ensure_csdm_steam_launch,
    ensure_steam_appid,
)


def test_ensure_steam_appid_writes_730(tmp_path):
    for rel in ("game/bin/win64", "game/csgo", "game"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    written = ensure_steam_appid(tmp_path)
    assert len(written) == 3
    for path in written:
        assert path.read_text(encoding="ascii").strip() == "730"
    assert ensure_steam_appid(tmp_path) == []


def test_block_steam_overlay_renames_dlls(tmp_path):
    (tmp_path / "GameOverlayRenderer64.dll").write_bytes(b"x")
    (tmp_path / "GameOverlayRenderer.dll").write_bytes(b"y")
    blocked = block_steam_overlay(tmp_path)
    assert {p.name for p in blocked} == {
        "GameOverlayRenderer64.dll.blocked",
        "GameOverlayRenderer.dll.blocked",
    }
    assert not (tmp_path / "GameOverlayRenderer64.dll").exists()
    assert (tmp_path / "GameOverlayRenderer64.dll.blocked").is_file()
    assert block_steam_overlay(tmp_path) == []


def test_ensure_csdm_steam_launch_merges_flags(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"playback": {"launchParameters": "-sw"}}), encoding="utf-8")
    assert ensure_csdm_steam_launch(settings) is True
    params = json.loads(settings.read_text(encoding="utf-8"))["playback"]["launchParameters"]
    assert "-sw" in params.split()
    assert "-steam" in params.split()
    assert "-insecure" in params.split()
    assert ensure_csdm_steam_launch(settings) is False
