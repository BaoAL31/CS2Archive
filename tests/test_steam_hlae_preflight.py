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
        assert path.read_bytes() == b"730\n"
    assert ensure_steam_appid(tmp_path) == []


def test_ensure_steam_appid_rewrites_trailing_nul(tmp_path):
    win64 = tmp_path / "game" / "bin" / "win64"
    win64.mkdir(parents=True)
    path = win64 / "steam_appid.txt"
    path.write_bytes(b"730\n\x00")
    written = ensure_steam_appid(tmp_path)
    assert path in written
    assert path.read_bytes() == b"730\n"


def test_block_steam_overlay_renames_dlls(tmp_path):
    (tmp_path / "GameOverlayRenderer64.dll").write_bytes(b"x")
    (tmp_path / "GameOverlayRenderer.dll").write_bytes(b"y")
    (tmp_path / "SteamOverlayVulkanLayer64.dll").write_bytes(b"vk")
    blocked = block_steam_overlay(tmp_path)
    assert {p.name for p in blocked} == {
        "GameOverlayRenderer64.dll.blocked",
        "GameOverlayRenderer.dll.blocked",
        "SteamOverlayVulkanLayer64.dll.blocked",
    }
    assert not (tmp_path / "GameOverlayRenderer64.dll").exists()
    assert not (tmp_path / "SteamOverlayVulkanLayer64.dll").exists()
    assert (tmp_path / "GameOverlayRenderer64.dll.blocked").is_file()
    assert block_steam_overlay(tmp_path) == []


def test_block_steam_overlay_renames_bin_copies(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "GameOverlayRenderer64.dll").write_bytes(b"x")
    blocked = block_steam_overlay(tmp_path)
    assert blocked == [bin_dir / "GameOverlayRenderer64.dll.blocked"]
    assert not (bin_dir / "GameOverlayRenderer64.dll").exists()


def test_ensure_csdm_steam_launch_merges_flags(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"playback": {"launchParameters": "-sw"}}), encoding="utf-8")
    assert ensure_csdm_steam_launch(settings) is True
    params = json.loads(settings.read_text(encoding="utf-8"))["playback"]["launchParameters"]
    tokens = params.split()
    assert "-sw" in tokens
    assert "-steam" in tokens
    assert "-insecure" in tokens
    assert "-allow_third_party_software" in tokens
    assert "-nominidumps" in tokens
    assert "-nobreakpad" in tokens
    assert "-nocrashdialog" in tokens
    assert tokens[tokens.index("+sv_lan") + 1] == "1"
    assert ensure_csdm_steam_launch(settings) is False
