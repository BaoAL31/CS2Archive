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
