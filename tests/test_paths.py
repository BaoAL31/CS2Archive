"""Renders layout: pov homes, hook/intro/shorts placement, purge sparing."""

from __future__ import annotations

import json
from pathlib import Path

from cs2archive import paths
from cs2archive.paths import (
    find_action_timeline,
    find_match_strips,
    find_pov_dirs,
    find_short_timelines,
    hook_dir,
    intro_dir,
    pov_action_timeline,
    pov_dir,
    purge_pov_dir,
    shorts_base,
    strips_base,
)


def _fake_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "RENDERS_DIR", tmp_path / "renders")
    return tmp_path / "renders"


def test_pov_dir_naming(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "RENDERS_DIR", tmp_path / "renders")
    assert pov_dir("demo-stem", "kyousuke") == \
        tmp_path / "renders" / "pov-demo-stem_kyousuke"
    assert pov_dir("stem", "Kyo Suke!") == \
        tmp_path / "renders" / "pov-stem_Kyo_Suke"


def test_subdirs_live_under_pov():
    pov = pov_dir("stem", "kyousuke")
    assert hook_dir(pov) == pov / "hook"
    assert intro_dir(pov) == pov / "intro"
    assert shorts_base(pov) == pov / "shorts"
    assert strips_base(pov) == pov / "stat-strips"
    assert pov_action_timeline(pov) == pov / "action_timeline.json"


def test_find_match_strips_prefers_pov_copy(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    legacy = renders / "stat-strips" / "m1"
    legacy.mkdir(parents=True)
    (legacy / "repeek_left.png").write_text("L")
    (legacy / "repeek_right.png").write_text("R")
    assert find_match_strips("m1") == [legacy]
    pov = renders / "pov-stem_nick" / "stat-strips" / "m1"
    pov.mkdir(parents=True)
    (pov / "repeek_left.png").write_text("L")
    (pov / "repeek_right.png").write_text("R")
    assert find_match_strips("m1") == [pov, legacy]


def test_find_match_strips_incomplete_ignored(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    half = renders / "pov-stem_nick" / "stat-strips" / "m1"
    half.mkdir(parents=True)
    (half / "repeek_left.png").write_text("L")
    assert find_match_strips("m1") == []


def test_find_pov_dirs_lists_nicks(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    (renders / "pov-stem_a").mkdir(parents=True)
    (renders / "pov-stem_b").mkdir(parents=True)
    (renders / "pov-other_c").mkdir(parents=True)
    (renders / "hl-stem").mkdir(parents=True)
    assert find_pov_dirs("stem") == [renders / "pov-stem_a",
                                     renders / "pov-stem_b"]


def test_find_action_timeline_prefers_pov_copy(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    hl = renders / "hl-stem" / "action_timeline.json"
    hl.parent.mkdir(parents=True)
    hl.write_text("{}")
    assert find_action_timeline("stem") == hl
    pov = renders / "pov-stem_nick" / "action_timeline.json"
    pov.parent.mkdir(parents=True)
    pov.write_text("{}")
    assert find_action_timeline("stem") == pov


def test_find_action_timeline_missing_is_none(monkeypatch, tmp_path):
    _fake_renders(monkeypatch, tmp_path)
    assert find_action_timeline("nope") is None


def test_find_short_timelines_new_and_legacy(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    new = renders / "pov-stem_nick" / "shorts" / "shorts-a" / "short_timeline.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    old = renders / "shorts" / "shorts-stem" / "shorts-b" / "short_timeline.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    assert find_short_timelines("stem") == [new, old]


def test_purge_spares_shorts(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    pov = renders / "pov-stem_nick"
    (pov / "shorts" / "shorts-a").mkdir(parents=True)
    (pov / "shorts" / "shorts-a" / "short_timeline.json").write_text("{}")
    (pov / "hook").mkdir(parents=True)
    (pov / "hook" / "hook.mp4").write_bytes(b"x" * 2048)
    (pov / "combined.mp4").write_bytes(b"x" * 1024)
    gb = purge_pov_dir(pov)
    assert gb > 0
    assert (pov / "shorts" / "shorts-a" / "short_timeline.json").is_file()
    assert not (pov / "hook").exists()
    assert not (pov / "combined.mp4").exists()
    assert pov.is_dir()  # the pov home itself survives while shorts remain


def test_purge_empty_pov_dir_removed(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    pov = renders / "pov-stem_nick"
    (pov / "hook").mkdir(parents=True)
    purge_pov_dir(pov)
    assert not pov.exists()


def test_purge_dry_run_changes_nothing(monkeypatch, tmp_path):
    renders = _fake_renders(monkeypatch, tmp_path)
    pov = renders / "pov-stem_nick"
    (pov / "hook").mkdir(parents=True)
    (pov / "hook" / "hook.mp4").write_bytes(b"x" * 1024)
    purge_pov_dir(pov, dry_run=True)
    assert (pov / "hook" / "hook.mp4").is_file()


# ── backlog shorts grouping ─────────────────────────────────────────

def _short(sid="SID1", nick="kyousuke", tick=1000):
    return {"short_type": "4k", "pov_steam_id": sid, "pov_nick": nick,
            "kill_ticks": [tick, tick + 1, tick + 2, tick + 3],
            "start_tick": tick - 100, "end_tick": tick + 100}


def test_persist_shorts_grouped_by_card_nick(monkeypatch, tmp_path):
    import cs2archive._backlog_common as bc
    from cs2archive._backlog_common import persist_shorts_grouped

    monkeypatch.setattr(paths, "RENDERS_DIR", tmp_path / "renders")
    demo = tmp_path / "demos" / "faceit" / "m.dem"
    demo.parent.mkdir(parents=True)
    demo.write_text("")
    timeline = {"demo_path": str(demo), "map": "Nuke", "tickrate": 64,
                "kills": [], "shorts": []}
    out = persist_shorts_grouped(
        demo, timeline, [_short("SID1", "kyousuke", 1000),
                         _short("SID2", "m0NESY", 2000)],
        {"SID1": "kyousuke", "SID2": "m0NESY"})
    assert out == 2
    base1 = tmp_path / "renders" / "pov-m_kyousuke" / "shorts"
    base2 = tmp_path / "renders" / "pov-m_m0NESY" / "shorts"
    assert (base1 / "shorts-4k_multikill-kyousuke-t900" / "short_timeline.json").is_file()
    assert (base2 / "shorts-4k_multikill-m0NESY-t1900" / "short_timeline.json").is_file()
    # shared kill-list cache persisted once per involved pov folder
    assert (base1 / "action_timeline.json").is_file()
    assert (base2 / "action_timeline.json").is_file()


def test_persist_shorts_unmapped_falls_back_legacy(monkeypatch, tmp_path, capsys):
    from cs2archive._backlog_common import persist_shorts_grouped

    monkeypatch.setattr(paths, "RENDERS_DIR", tmp_path / "renders")
    import cs2archive.shorts as _shorts_pkg
    monkeypatch.setattr(_shorts_pkg, "RENDERS_DIR", tmp_path / "renders")
    monkeypatch.setattr("cs2archive._backlog_common.load_accounts_by_steam",
                        lambda: {})
    demo = tmp_path / "demos" / "faceit" / "m.dem"
    demo.parent.mkdir(parents=True)
    demo.write_text("")
    timeline = {"demo_path": str(demo), "map": "Nuke", "tickrate": 64,
                "kills": [], "shorts": []}
    out = persist_shorts_grouped(demo, timeline, [_short("GHOST", "?")], {})
    assert out == 1
    assert "WARN" in capsys.readouterr().out


def test_card_nicks_for_demo(monkeypatch, tmp_path):
    import cs2archive._backlog_common as bc
    from cs2archive._backlog_common import card_nicks_for_demo

    monkeypatch.setattr(bc, "BACKLOG_DIR", tmp_path / "backlog")
    card = tmp_path / "backlog" / "match" / "high" / "a.json"
    card.parent.mkdir(parents=True)
    card.write_text(json.dumps({"player": "kyousuke", "steam_id": "SID1",
                                "demo_path": "demos/faceit/m.dem"}))
    other = tmp_path / "backlog" / "match" / "high" / "b.json"
    other.write_text(json.dumps({"player": "nope", "steam_id": "SID9",
                                 "demo_path": "demos/faceit/other.dem"}))
    demo = tmp_path / "demos" / "faceit" / "m.dem"
    assert card_nicks_for_demo(demo) == {"SID1": "kyousuke"}


# ── hook home ───────────────────────────────────────────────────────

def test_hook_run_dir_under_pov_dir():
    from cs2archive.pov.build_hook_timeline import hook_run_dir

    pov = pov_dir("stem", "kyousuke")
    assert hook_run_dir(Path("x/stem.dem"), "whatever", pov_dir=pov) == pov / "hook"


def test_hook_run_dir_derived_matches_pipeline():
    from cs2archive.pov.build_hook_timeline import hook_run_dir

    # bare sid resolves through player_accounts only when available; without
    # accounts it falls back to the raw id — either way under a pov- dir.
    out = hook_run_dir(Path("x/stem.dem"), "76561199032006224")
    assert out.parent.name.startswith("pov-stem_")
    assert out.name == "hook"
