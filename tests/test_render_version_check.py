"""Tests for pre-render version gate (local-only, no network)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "pov"))

from render_version_check import (
    RenderVersionError,
    assert_render_versions,
    check_render_versions,
    format_version,
    normalize_patch_version,
    parse_steam_inf_patch,
    version_at_least,
)


def test_normalize_patch_dotted():
    assert normalize_patch_version("1.41.7.2") == "1.41.7.2"


def test_normalize_patch_undotted_demoparser():
    assert normalize_patch_version("14172") == "1.41.7.2"
    assert normalize_patch_version("14168") == "1.41.6.8"


def test_normalize_patch_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_patch_version("abc")


def test_parse_steam_inf_patch():
    text = "ClientVersion=1\nPatchVersion=1.41.7.2\nProductName=cs2\n"
    assert parse_steam_inf_patch(text) == "1.41.7.2"


def test_version_at_least():
    assert version_at_least((2, 192, 0, 0), (2, 192, 0))
    assert version_at_least((2, 192, 1), (2, 192, 0))
    assert not version_at_least((2, 190, 2), (2, 192, 0))
    assert version_at_least((3, 20, 0), (3, 20, 0))


def test_format_version_trims_revision():
    assert format_version((3, 20, 0, 0)) == "3.20.0"
    assert format_version((2, 192, 0, 0)) == "2.192.0"


def test_check_demo_game_mismatch(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.2\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")

    with (
        patch("render_version_check.read_demo_patch", return_value="1.41.6.8"),
        patch("render_version_check.read_pe_version", return_value=(2, 192, 0, 0)),
    ):
        result = check_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            min_hlae=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert not result.ok
    assert any(c == "RENDER_DEMO_GAME_MISMATCH" for c, _ in result.errors)


def test_check_hlae_outdated(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.2\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")

    def pe_ver(path: Path):
        if path == hlae:
            return (2, 190, 2, 0)
        return (3, 20, 0, 0)

    with patch("render_version_check.read_pe_version", side_effect=pe_ver):
        result = check_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            min_hlae=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert not result.ok
    assert any(c == "RENDER_HLAE_OUTDATED" for c, _ in result.errors)


def test_assert_ok(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.2\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")

    with (
        patch("render_version_check.read_demo_patch", return_value="1.41.7.2"),
        patch("render_version_check.read_pe_version", return_value=(9, 9, 9, 0)),
    ):
        vers = assert_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            min_hlae=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert vers["demo"] == "1.41.7.2"
    assert vers["cs2"] == "1.41.7.2"


def test_assert_raises(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.2\n", encoding="utf-8")
    with pytest.raises(RenderVersionError) as ei:
        assert_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=tmp_path / "missing-hlae.exe",
            csdm_exe=tmp_path / "missing-csdm.exe",
        )
    assert ei.value.code in ("RENDER_HLAE_MISSING", "RENDER_VERSION_CHECK")
