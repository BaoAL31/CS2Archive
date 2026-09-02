"""Popular-event filter for Allstar/HLTV Clip Observation scrape."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.popular_events import is_popular_event


def test_blast_porto_and_ewc_and_major_kept():
    assert is_popular_event("blast-open-porto-2026")
    assert is_popular_event("esports-world-cup-2026")
    assert is_popular_event("iem-cologne-major-2026")
    assert is_popular_event("pgl-major-2026")
    assert is_popular_event("iem-beijing-2026")


def test_cct_and_qualifiers_dropped():
    assert not is_popular_event("cct-season-3")
    assert not is_popular_event("blast-open-porto-2026-closed-qualifier")
    assert not is_popular_event("esl-challenger-league")
    assert not is_popular_event("iem-cologne-major-2026-european-qualifier")
    assert not is_popular_event("blast-open-singapore-2027")
    assert not is_popular_event("circuit-x-blast-open-porto-2026-north-america-rising-event")
