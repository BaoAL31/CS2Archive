"""Util-cam demo resolution: the match-id prefix wins, ambiguity is loud.

Real failure this pins: the flight render resolved
``2398739-vitality-vs-falcons-m2-dust2`` by demo *file name* and returned
``demos/hltv/2393236-vitality-vs-falcons-iem-rio/vitality-vs-falcons-m2-dust2.dem``
(an old-build demo). CS2 refused it as a demo version mismatch while the POV
render — which uses the card's exact demo path — worked fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cs2archive.overlay import render_util_cams
from cs2archive.overlay.overlay_utilcams import _run_batch_util_cams_subprocess

STEM = "vitality-vs-falcons-m2-dust2"
CURRENT = "2398739-vitality-vs-falcons-esl-pro-league-season-24"
STALE = "2393236-vitality-vs-falcons-iem-rio"


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Two matches sharing a demo file name; returns (util_cams_root, current, stale)."""
    stale = tmp_path / "demos" / "hltv" / STALE / f"{STEM}.dem"
    current = tmp_path / "demos" / "hltv" / CURRENT / f"{STEM}.dem"
    for path in (stale, current):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"dem")
    util_cams_root = tmp_path / "renders" / "pov-x" / "utility_cams"
    util_cams_root.mkdir(parents=True, exist_ok=True)
    return util_cams_root, current, stale


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(render_util_cams, "_DEMO_PATH_OVERRIDE", None)
    monkeypatch.setattr(render_util_cams, "_DEMOS_DIR", None)


def test_match_id_prefix_picks_the_right_demo(tmp_path: Path) -> None:
    root, current, _stale = _tree(tmp_path)

    assert render_util_cams._find_demo_for_id(root, f"2398739-{STEM}") == current.resolve()


def test_ambiguous_prefix_is_an_error_not_a_coin_flip(tmp_path: Path) -> None:
    """Two folders can still share a match id prefix — that must be loud."""
    root, current, _stale = _tree(tmp_path)
    dupe = tmp_path / "demos" / "hltv" / f"{CURRENT}-split" / f"{STEM}.dem"
    dupe.parent.mkdir(parents=True, exist_ok=True)
    dupe.write_bytes(b"dem")

    with pytest.raises(ValueError) as excinfo:
        render_util_cams._find_demo_for_id(root, f"2398739-{STEM}")

    message = str(excinfo.value)
    assert "--demo-path" in message
    assert str(current.resolve()) in message
    assert str(dupe.resolve()) in message


def test_bare_stem_never_scans_other_matches(tmp_path: Path) -> None:
    """No match id → no search. It must not return another match's demo."""
    root, current, stale = _tree(tmp_path)

    resolved = render_util_cams._find_demo_for_id(root, STEM)

    assert resolved != current.resolve()
    assert resolved != stale.resolve()
    assert not resolved.is_file()


def test_faceit_flat_lookup(tmp_path: Path) -> None:
    root, _current, _stale = _tree(tmp_path)
    flat = tmp_path / "demos" / "faceit" / "1-abc-def-mirage.dem"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"dem")

    assert render_util_cams._find_demo_for_id(root, "1-abc-def-mirage") == flat.resolve()


def test_duplicate_root_via_junction_is_deduped(tmp_path: Path, monkeypatch) -> None:
    """The store is reachable twice (demos/hltv + CS2UtilArchive junction)."""
    root, current, _stale = _tree(tmp_path)
    monkeypatch.setattr(render_util_cams, "_DEMOS_DIR", tmp_path / "demos" / "hltv")

    assert render_util_cams._find_demo_for_id(root, f"2398739-{STEM}") == current.resolve()


def test_demo_path_override_wins(tmp_path: Path) -> None:
    root, current, stale = _tree(tmp_path)
    render_util_cams._DEMO_PATH_OVERRIDE = stale.resolve()

    # The override is applied where the job is built, not inside the resolver.
    assert render_util_cams._DEMO_PATH_OVERRIDE != current.resolve()


def test_subprocess_command_forwards_demo_path(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}
    demo = tmp_path / "vitality-vs-falcons-m2-dust2.dem"
    demo.write_bytes(b"dem")

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd

        class _Done:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Done()

    monkeypatch.setattr("subprocess.run", fake_run)

    _run_batch_util_cams_subprocess(
        demo_path=demo,
        steam_id="76561197978835160",
        data_dir=tmp_path / "data" / f"demo=2398739-{STEM}",
        util_cams_root=tmp_path / "utility_cams",
        demo_data_dir_name=f"demo=2398739-{STEM}",
    )

    cmd = seen["cmd"]
    assert "--demo-path" in cmd
    assert cmd[cmd.index("--demo-path") + 1] == str(demo.resolve())
