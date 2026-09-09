from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "faceit"))

from daily_notable import fallback_demo_if_history_lagged


class _FakeStat:
    def __init__(self, mtime: float):
        self.st_mtime = mtime


class _FakeDem(Path):
    _flavour = type(Path())._flavour

    def __init__(self, name: str, mtime: float):
        super().__init__(name)
        self._mtime = mtime

    def stat(self, *, follow_symlinks: bool = True):
        return _FakeStat(self._mtime)


def test_fallback_skips_wrong_map_leftover():
    now = 1_000_000.0
    leftover = _FakeDem("team_a vs team_b - mirage.dem", now - 10)
    got = fallback_demo_if_history_lagged(
        "dust2", now=now, demos=[leftover],
    )
    assert got is None


def test_fallback_accepts_fresh_matching_map():
    now = 1_000_000.0
    fresh = _FakeDem("team_a vs team_b - dust2.dem", now - 10)
    old = _FakeDem("team_a vs team_b - dust2.dem", now - 10_000)
    got = fallback_demo_if_history_lagged(
        "dust2", now=now, demos=[old, fresh],
    )
    assert got is fresh
