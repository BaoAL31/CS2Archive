"""Shorts title opponent / top-10 display."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.make_short_meta import _is_top10_opponent, _make_title


def test_vitality_is_top10():
    assert _is_top10_opponent("Vitality")


def test_onic_4k_title_names_vitality():
    title, _fmt = _make_title(
        "onic", "4k", "Vitality", "Cache", "#blastopenporto2026",
        clutch=None, kills=4, punch_tags=[], start_tick=228005,
    )
    assert "Vitality" in title
    assert "onic" in title
    assert "Vita" not in title.replace("Vitality", "")


def test_wallbang_title_names_kind_and_opponent():
    title, _fmt = _make_title(
        "koala", "wallbang", "MOUZ", "Inferno", "",
        clutch=None, kills=1, punch_tags=[], start_tick=1,
    )
    assert "koala" in title
    assert "wallbang" in title.lower()
    assert "MOUZ" in title
    assert "4K" not in title


def test_knife_title_names_kind_and_opponent():
    title, _fmt = _make_title(
        "FalleN", "knife", "FaZe", "Mirage", "",
        clutch=None, kills=1, punch_tags=[], start_tick=1,
    )
    assert "FalleN" in title
    assert "knife" in title.lower()
    assert "FaZe" in title
    assert "4K" not in title


def test_defuse_title_names_kind_and_opponent():
    title, _fmt = _make_title(
        "bLitz", "defuse", "Vitality", "Mirage", "",
        clutch=None, kills=0, punch_tags=[], start_tick=1,
    )
    assert "bLitz" in title
    assert "defuse" in title.lower()
    assert "Vitality" in title
    assert "4K" not in title


def test_perfect_shots_title_names_kind_and_opponent():
    title, _fmt = _make_title(
        "ZywOo", "perfect_shots", "NAVI", "Dust2", "",
        clutch=None, kills=2, punch_tags=[], start_tick=1,
    )
    assert "ZywOo" in title
    assert "perfect" in title.lower()
    assert "NAVI" in title
    assert "4K" not in title


def test_flick_title_names_kind_and_opponent():
    title, _fmt = _make_title(
        "m0NESY", "flick", "Vitality", "Mirage", "",
        clutch=None, kills=1, punch_tags=[], start_tick=1,
    )
    assert "m0NESY" in title
    assert "flick" in title.lower()
    assert "Vitality" in title
    assert "4K" not in title
    assert "quickscope" not in title.lower()


