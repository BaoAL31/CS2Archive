"""resolve_pov_strips_dirs: captures land in per-POV folders, never a shared tree."""

from __future__ import annotations

import json
from pathlib import Path

import scrapers.repeek_snapshot as rs
from cs2archive.paths import pov_dir


def _write_card(root: Path, sub: str, name: str, faceit_match_id: str,
                demo_path: str, player: str) -> None:
    card = root / "backlog" / "faceit" / sub / name
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text(json.dumps({
        "faceit_match_id": faceit_match_id,
        "demo_path": demo_path,
        "player": player,
    }), encoding="utf-8")


def test_backlog_cards_resolve_pov_dirs(tmp_path):
    mid = "1-aaaa-bbbb"
    _write_card(tmp_path, "2026-10-08/high", "donk-card.json", mid,
                "demos/faceit/team_A vs team_B - mirage.dem", "donk")
    _write_card(tmp_path, "2026-10-08/high", "m0nesy-card.json", mid,
                "demos/faceit/team_A vs team_B - mirage.dem", "m0nesy")
    dirs = rs.resolve_pov_strips_dirs(mid, project_root=tmp_path)
    assert [d.name for d in dirs] == [mid, mid]
    parents = sorted(d.parent.parent.name for d in dirs)
    assert parents == [
        "pov-team_A vs team_B - mirage_donk",
        "pov-team_A vs team_B - mirage_m0nesy",
    ]
    assert all(str(d).startswith(str(tmp_path)) for d in dirs)


def test_no_cards_no_stem_resolves_nothing(tmp_path):
    assert rs.resolve_pov_strips_dirs("1-nope", project_root=tmp_path) == []


def test_api_roster_resolves_via_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rs, "_fetch_faceit_rosters",
        lambda match_id: [("fid-1", "donk666"), ("fid-2", "rando")],
    )
    import cs2archive._backlog_common as bc
    monkeypatch.setattr(
        bc, "load_accounts_by_steam",
        lambda: {"765": {"nickname": "donk", "faceit_id": "fid-1",
                         "faceit_nickname": "donk666"}},
    )
    dirs = rs.resolve_pov_strips_dirs(
        "1-bbbb", dem_stem="team_A vs team_B - mirage", project_root=tmp_path)
    assert len(dirs) == 1
    want = (tmp_path / "renders"
            / pov_dir("team_A vs team_B - mirage", "donk").name
            / "stat-strips" / "1-bbbb")
    assert dirs[0] == want


def test_fanout_copies_panes(tmp_path):
    src = tmp_path / "pov-a" / "stat-strips" / "1-x"
    src.mkdir(parents=True)
    for name in ("repeek_left.png", "repeek_right.png", "repeek_meta.json"):
        (src / name).write_bytes(b"data-" + name.encode())
    dst = tmp_path / "pov-b" / "stat-strips" / "1-x"
    rs.fanout_panes(src, [src, dst])
    assert (dst / "repeek_left.png").read_bytes() == b"data-repeek_left.png"
    assert (dst / "repeek_meta.json").is_file()
