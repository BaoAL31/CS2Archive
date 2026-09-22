from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

from build_stat_strips import (
    aggregate_window,
    display_nicks,
    fmt_matches,
    fmt_swing,
    fmt_wins,
    level_icon_path,
    match_rating,
    parse_rounds,
    rating_is_hot,
)
from faceit_names import nick_alias_parts


def test_level_icons_resolve_for_all_levels():
    for lvl in range(1, 11):
        p = level_icon_path(lvl)
        assert p is not None and p.exists(), lvl
    assert level_icon_path(0) is None
    assert level_icon_path(99) is None
    assert level_icon_path(None) is None


def test_parse_rounds():
    assert parse_rounds("13 / 11") == 24
    assert parse_rounds("16 / 14") == 30
    assert parse_rounds("") == 0
    assert parse_rounds("?") == 0


def test_match_rating_known_line():
    # k=25 d=15 r=24, 4 doubles (3 exact) + 1 triple:
    # KR=1.5341 SR=1.1830 MKR=1.2074 -> 1.3221
    assert match_rating(25, 15, 24, 4, 1, 0, 0) == pytest.approx(1.3221, abs=1e-3)
    assert match_rating(0, 0, 0, 0, 0, 0, 0) == 0.0
    assert match_rating(10, 10, 20, 0, 0, 0, 0) > 0.5


def test_aggregate_window_means_and_swing():
    def game(k, d, won=True):
        return {"k": k, "d": d, "a": 4, "adr": 90.0, "kr": 0.8,
                "rounds": 24, "rating": 1.2, "won": won}

    games = [game(20, 10), game(20, 10), game(10, 10, False), game(10, 10)]
    agg = aggregate_window(games)
    assert agg["n"] == 4
    assert agg["wins_pct"] == 75.0
    assert agg["k_avg"] == 15.0
    assert agg["kd"] == pytest.approx(60 / 40)
    assert agg["swing"] == pytest.approx(100.0)
    assert aggregate_window([]) == {"n": 0}


def test_aggregate_swing_guards_zero_denominator():
    def game(k, d):
        return {"k": k, "d": d, "a": 0, "adr": 0.0, "kr": 0.0,
                "rounds": 0, "rating": 0.0, "won": False}

    agg = aggregate_window([game(5, 0), game(0, 0)])
    assert agg["swing"] == 0.0


def test_strip_formatters():
    assert fmt_matches(7698) == "7,698"
    assert fmt_matches(12690) == "12,690"
    assert fmt_wins(70.4) == "%70"
    assert fmt_swing(4.96).startswith("+%")
    assert fmt_swing(-0.75).startswith("-%")
    assert rating_is_hot(1.41)
    assert not rating_is_hot(1.16)


def test_nick_alias_parts_skips_when_live_contains_real_name():
    assert nick_alias_parts("donk666", "donk") == ("donk666", None)
    assert nick_alias_parts("s1mplecsgod", "s1mple") == ("s1mplecsgod", None)
    assert nick_alias_parts("teses", "TeSeS") == ("teses", None)
    assert nick_alias_parts("ZywOo", "ZywOo") == ("ZywOo", None)


def test_nick_alias_parts_returns_real_name_separately():
    assert nick_alias_parts("holaaaa", "s1mple") == ("holaaaa", "s1mple")
    assert nick_alias_parts("n1oz", "n1oz") == ("n1oz", None)


def test_display_nicks_uses_form_fields():
    assert display_nicks({"nick": "donk666", "fid": "nope"}) == ("donk666", None)
