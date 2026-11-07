"""HLAE must adapt to whatever CS2 version Steam serves.

Three CS2 patches in a row (1.41.9.3→.4→.5) the RENDER_CS2_UNPINNED gate added
zero information: every time the answer was "run the HLAE we already have".
Worse, a Steam ROLLBACK (1.41.9.5 → 1.41.9.2 mid-hour on 2026-11-04) left the
box on an older CS2 whose correct HLAE (2.192.8) was also installed but not
selected. So: selection among installed HLAEs is now automatic, and CSDM's
settings.json is repointed before every render.
"""
import json
from pathlib import Path
from unittest.mock import patch

from cs2archive.pov.render_version_check import (
    ensure_hlae_for_cs2,
    installed_hlae_versions,
    select_hlae_for_cs2,
)


def _make_installed(root: Path, versions: list[str]) -> None:
    for v in versions:
        d = root / f"HLAE-{v}"
        d.mkdir(parents=True)
        (d / "HLAE.exe").write_bytes(b"x")


def _fake_pe(versions_by_path):
    def read(path):
        for v, p in versions_by_path.items():
            if str(path) == str(p):
                return tuple(int(x) for x in v.split("."))
        raise AssertionError(f"unexpected path {path}")
    return read


def test_installed_scan_finds_dirs_and_versions(tmp_path):
    _make_installed(tmp_path, ["2.192.8", "2.192.10"])
    with patch("cs2archive.pov.render_version_check.read_pe_version",
               side_effect=_fake_pe({"2.192.8": tmp_path / "HLAE-2.192.8" / "HLAE.exe",
                                     "2.192.10": tmp_path / "HLAE-2.192.10" / "HLAE.exe"})):
        found = installed_hlae_versions(tmp_path)
    assert [v for v, _ in found] == [(2, 192, 8), (2, 192, 10)]


def test_select_picks_row_hlae_for_rolled_back_cs2(tmp_path):
    """CS2 1.41.9.2 needs 2.192.8 even though 2.192.10 is also installed."""
    _make_installed(tmp_path, ["2.192.8", "2.192.10"])
    with patch("cs2archive.pov.render_version_check.read_pe_version",
               side_effect=_fake_pe({"2.192.8": tmp_path / "HLAE-2.192.8" / "HLAE.exe",
                                     "2.192.10": tmp_path / "HLAE-2.192.10" / "HLAE.exe"})):
        sel = select_hlae_for_cs2("1.41.9.2", tmp_path)
    ver, path, how = sel
    assert ver == (2, 192, 8)
    assert how == "pin"


def test_select_picks_newest_installed_when_cs2_unpinned(tmp_path):
    """CS2 newer than every pin: the newest installed HLAE is the only option."""
    _make_installed(tmp_path, ["2.192.8", "2.192.10"])
    with patch("cs2archive.pov.render_version_check.read_pe_version",
               side_effect=_fake_pe({"2.192.8": tmp_path / "HLAE-2.192.8" / "HLAE.exe",
                                     "2.192.10": tmp_path / "HLAE-2.192.10" / "HLAE.exe"})):
        ver, path, how = select_hlae_for_cs2("1.41.9.9", tmp_path)
    assert ver == (2, 192, 10)
    assert how == "newest-installed"


def test_select_prefers_newest_within_range(tmp_path):
    """CS2 1.41.9.4 spans 2.192.8..2.192.10: pick the newest in range."""
    _make_installed(tmp_path, ["2.192.6", "2.192.8", "2.192.10"])
    with patch("cs2archive.pov.render_version_check.read_pe_version",
               side_effect=_fake_pe({v: tmp_path / f"HLAE-{v}" / "HLAE.exe"
                                     for v in ["2.192.6", "2.192.8", "2.192.10"]})):
        ver, _p, how = select_hlae_for_cs2("1.41.9.4", tmp_path)
    assert ver == (2, 192, 10)
    assert how == "pin"


def test_select_returns_none_when_nothing_installed(tmp_path):
    assert select_hlae_for_cs2("1.41.9.2", tmp_path) is None


def test_ensure_repoints_csdm_and_backs_up(tmp_path):
    """Settings pointing at 2.192.10 while CS2 is 1.41.9.2 must be repointed."""
    _make_installed(tmp_path / "versions", ["2.192.8", "2.192.10"])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "video": {"hlae": {
            "customLocationEnabled": True,
            "customExecutableLocation": str(tmp_path / "versions" / "HLAE-2.192.10" / "HLAE.exe"),
        }},
    }), encoding="utf-8")
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.9.2\n", encoding="utf-8")

    pe = _fake_pe({"2.192.8": tmp_path / "versions" / "HLAE-2.192.8" / "HLAE.exe",
                   "2.192.10": tmp_path / "versions" / "HLAE-2.192.10" / "HLAE.exe"})
    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=pe):
        chosen = ensure_hlae_for_cs2(
            steam_inf=steam_inf,
            installed_root=tmp_path / "versions",
            csdm_settings=settings,
        )

    assert chosen is not None and chosen[0] == (2, 192, 8)
    updated = json.loads(settings.read_text(encoding="utf-8"))
    hlae = updated["video"]["hlae"]
    assert hlae["customLocationEnabled"] is True
    assert Path(hlae["customExecutableLocation"]).name == "HLAE.exe"
    assert "HLAE-2.192.8" in hlae["customExecutableLocation"]
    backups = list(tmp_path.glob("settings.json.bak-hlae-auto-*"))
    assert backups, "settings.json must be backed up before rewriting"


def test_ensure_is_a_noop_when_already_correct(tmp_path):
    _make_installed(tmp_path / "versions", ["2.192.8"])
    settings = tmp_path / "settings.json"
    correct = str(tmp_path / "versions" / "HLAE-2.192.8" / "HLAE.exe")
    settings.write_text(json.dumps({
        "video": {"hlae": {"customLocationEnabled": True,
                           "customExecutableLocation": correct}},
    }), encoding="utf-8")
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.9.2\n", encoding="utf-8")

    pe = _fake_pe({"2.192.8": tmp_path / "versions" / "HLAE-2.192.8" / "HLAE.exe"})
    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=pe):
        result = ensure_hlae_for_cs2(
            steam_inf=steam_inf,
            installed_root=tmp_path / "versions",
            csdm_settings=settings,
        )
    assert result is not None
    assert list(tmp_path.glob("settings.json.bak-hlae-auto-*")) == [], \
        "no rewrite, no backup"
    assert json.loads(settings.read_text(encoding="utf-8"))["video"]["hlae"][
        "customExecutableLocation"] == correct
