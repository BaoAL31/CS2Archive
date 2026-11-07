"""Tests for pre-render version gate (local-only, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


from cs2archive.pov.render_version_check import (
    INCOMPATIBLE_DEMO_PATCHES,
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
        patch("cs2archive.pov.render_version_check.read_demo_patch", return_value="1.41.8.1"),
        patch("cs2archive.pov.render_version_check.read_pe_version", return_value=(3, 20, 0, 0)),
    ):
        result = check_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert not result.ok
    assert any(c == "RENDER_DEMO_GAME_MISMATCH" for c, _ in result.errors)


def test_incompatible_old_demo_patch(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.8.1\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")
    old = next(iter(INCOMPATIBLE_DEMO_PATCHES))

    with (
        patch("cs2archive.pov.render_version_check.read_demo_patch", return_value=old),
        patch("cs2archive.pov.render_version_check.read_pe_version", return_value=(3, 20, 0, 0)),
    ):
        result = check_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert not result.ok
    assert any(c == "RENDER_DEMO_TOO_OLD" for c, _ in result.errors)


def test_demo_below_min_renderable_is_too_old(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.8.1\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")

    with (
        patch("cs2archive.pov.render_version_check.read_demo_patch", return_value="1.41.5.0"),
        patch("cs2archive.pov.render_version_check.read_pe_version", return_value=(3, 20, 0, 0)),
    ):
        result = check_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert not result.ok
    assert any(c == "RENDER_DEMO_TOO_OLD" for c, _ in result.errors)


def test_slightly_older_demo_is_allowed(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.8.1\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")
    demo = tmp_path / "match.dem"
    demo.write_bytes(b"PBDEMS2")

    with (
        patch("cs2archive.pov.render_version_check.read_demo_patch", return_value="1.41.6.4"),
        patch("cs2archive.pov.render_version_check.read_pe_version", return_value=(3, 20, 0, 0)),
    ):
        result = check_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert result.ok
    assert result.versions["demo"] == "1.41.6.4"


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

    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=pe_ver):
        result = check_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
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
        patch("cs2archive.pov.render_version_check.read_demo_patch", return_value="1.41.7.2"),
        patch("cs2archive.pov.render_version_check.read_pe_version", return_value=(9, 9, 9, 0)),
    ):
        vers = assert_render_versions(
            demo,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
            hlae_override=(2, 192, 0),
            min_csdm=(3, 20, 0),
        )
    assert vers["demo"] == "1.41.7.2"
    assert vers["cs2"] == "1.41.7.2"


def test_hlae_bounds_for_cs2_table():
    """Each pin row maps to (its HLAE, the NEXT newer row's HLAE); above the top
    row CS2 is unpinned.

    Derived from the table itself. This test was hand-copied once, then rotted
    into the known-failures baseline when later CS2 rows landed (the source
    gained 1.41.8.5/1.41.9.0 bounds while the expectations did not). Adding a
    row is what every game update requires, so a hardcoded expectation here is
    guaranteed to go stale.
    """
    from cs2archive.pov.render_version_check import (
        CS2_MIN_HLAE,
        MIN_HLAE,
        format_version,
        hlae_bounds_for_cs2,
    )

    rows = sorted(CS2_MIN_HLAE, key=lambda row: row[0])
    for i, (bound, hlae) in enumerate(rows):
        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
        # A ceiling equal to the floor is degenerate (the next row needs the
        # same HLAE) and must read as "no ceiling", not as "this HLAE is too
        # new" for the patch that requires it.
        ceiling = None if nxt == hlae else nxt
        assert hlae_bounds_for_cs2(format_version(bound)) == (hlae, ceiling)
        if i + 1 < len(rows):
            # A patch between this row and the next keeps this row's bounds — but
            # only when the next patch is not itself a row (rows sit one patch
            # apart in places, and that patch starts the next row's range).
            bump = bound[:-1] + (bound[-1] + 1,)
            if bump < rows[i + 1][0]:
                assert hlae_bounds_for_cs2(format_version(bump)) == (hlae, ceiling)

    # Older than every pin: the absolute floor, ceilinged by the first row.
    assert hlae_bounds_for_cs2("0.0.0.0") == (MIN_HLAE, rows[0][1])
    # One patch above every pin: unpinned (RENDER_CS2_UNPINNED).
    newest = rows[-1][0]
    beyond = newest[:-1] + (newest[-1] + 1,)
    assert hlae_bounds_for_cs2(format_version(beyond)) is None


def test_resolve_hlae_prefers_csdm_custom(tmp_path: Path):
    from cs2archive.pov.render_version_check import resolve_hlae_exe

    custom = tmp_path / "HLAE-custom" / "HLAE.exe"
    custom.parent.mkdir()
    custom.write_bytes(b"x")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "video": {"hlae": {
            "customLocationEnabled": True,
            "customExecutableLocation": str(custom),
        }},
    }), encoding="utf-8")
    fallback = tmp_path / "ProgramFiles" / "HLAE.exe"
    fallback.parent.mkdir()
    fallback.write_bytes(b"y")
    assert resolve_hlae_exe(csdm_settings=settings, fallback=fallback) == custom


def test_cs2_unpinned_now_proceeds_with_warning(tmp_path: Path):
    """CS2 above every pin no longer hard-fails.

    Three patches in a row (1.41.9.3→.4→.5) the RENDER_CS2_UNPINNED gate added
    no information — every time the answer was 'run the newest installed
    HLAE'. ensure_hlae_for_cs2() now does that automatically and the gate only
    warns (a wrong hook fails loudly downstream with Game error / Raw files
    not found, not silently).
    """
    from cs2archive.pov.render_version_check import CS2_MIN_HLAE, format_version

    newest = max(bound for bound, _ in CS2_MIN_HLAE)
    unpinned = newest[:-1] + (newest[-1] + 1,)

    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text(f"PatchVersion={format_version(unpinned)}\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")

    def fake_pe(path):
        # HLAE is genuinely old here (2.192.5); CSDM must read as current or
        # the unrelated RENDER_CSDM_OUTDATED error would mask what we pin.
        return (2, 192, 5, 0) if "HLAE" in str(path) else (3, 20, 1, 0)

    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=fake_pe):
        result = check_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
        )
    assert result.ok, f"unpinned CS2 proceeds: {result.errors}"


def test_hlae_cs2_mismatch_hard_fails(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.8.5\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")

    def pe_ver(path: Path):
        if path == hlae:
            return (2, 192, 4, 0)  # too old for 1.41.8.5
        return (3, 20, 0, 0)

    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=pe_ver):
        result = check_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
        )
    assert not result.ok
    assert any(c == "RENDER_HLAE_CS2_MISMATCH" for c, _ in result.errors)


def test_hlae_too_new_for_cs2_hard_fails(tmp_path: Path):
    from cs2archive.pov.render_version_check import CS2_MIN_HLAE, format_version

    # Derived from the pin table: a row whose ceiling is a strictly newer HLAE
    # build. That build was made for the next CS2 patch, so its hook looks for
    # symbols this patch does not have. Hardcoding (1.41.8.3, 2.192.5) here had
    # the same rot risk as the hand-copied bounds table that put
    # test_hlae_bounds_for_cs2_table in the known-failures baseline.
    rows = sorted(CS2_MIN_HLAE, key=lambda row: row[0])
    pair = next(
        ((rows[i][0], rows[i + 1][1]) for i in range(len(rows) - 1) if rows[i + 1][1] > rows[i][1]),
        None,
    )
    if pair is None:
        pytest.skip("every pin row shares one HLAE build — no 'too new' case exists")
    cs2_patch, too_new = pair

    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text(f"PatchVersion={format_version(cs2_patch)}\n", encoding="utf-8")
    hlae = tmp_path / "HLAE.exe"
    csdm = tmp_path / "cs-demo-manager.exe"
    hlae.write_bytes(b"x")
    csdm.write_bytes(b"x")

    def pe_ver(path: Path):
        if path == hlae:
            return (*too_new, 0)  # built for the next CS2 patch
        return (3, 20, 0, 0)

    with patch("cs2archive.pov.render_version_check.read_pe_version", side_effect=pe_ver):
        result = check_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=hlae,
            csdm_exe=csdm,
        )
    assert not result.ok
    assert any(c == "RENDER_HLAE_CS2_MISMATCH" for c, _ in result.errors)


def test_assert_raises(tmp_path: Path):
    steam_inf = tmp_path / "steam.inf"
    steam_inf.write_text("PatchVersion=1.41.7.2\n", encoding="utf-8")
    with pytest.raises(RenderVersionError) as ei:
        assert_render_versions(
            None,
            steam_inf=steam_inf,
            hlae_exe=tmp_path / "missing-hlae.exe",
            csdm_exe=tmp_path / "missing-csdm.exe",
            hlae_override=(2, 192, 0),
        )
    assert ei.value.code in ("RENDER_HLAE_MISSING", "RENDER_VERSION_CHECK")


def test_degenerate_ceiling_is_not_a_mismatch():
    """A CS2 patch sharing its HLAE with the next patch must accept that HLAE.

    The table's newest row (CS2 1.41.9.2 → HLAE 2.192.8) sits directly above
    1.41.9.1 → the same 2.192.8, because upstream had released nothing newer
    when 1.41.9.2 shipped. Read literally, 1.41.9.1's "ceiling" would then be
    2.192.8 and the gate would reject 2.192.8 as TOO NEW for the very patch
    that mandates it.
    """
    from cs2archive.pov.render_version_check import (
        CS2_MIN_HLAE,
        format_version,
        hlae_bounds_for_cs2,
    )

    rows = sorted(CS2_MIN_HLAE, key=lambda row: row[0])
    pair = next(((rows[i], rows[i + 1]) for i in range(len(rows) - 1)
                 if rows[i][1] == rows[i + 1][1]), None)
    assert pair is not None, "table no longer has a same-HLAE pair"
    (below_bound, below_hlae), (bound, hlae) = pair
    assert hlae_bounds_for_cs2(format_version(below_bound)) == (hlae, None)
    assert hlae_bounds_for_cs2(format_version(bound))[0] == hlae
