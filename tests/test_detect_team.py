"""Folder-slug org parsing for Shorts titles."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from shorts.detect_team import orgs_from_folder


def test_natus_vincere_vs_m80_does_not_eat_m80():
    demo = "demos/hltv/2396925-natus-vincere-vs-m80-blast-open-porto/natus-vincere-vs-m80-m3-inferno.dem"
    orgs = orgs_from_folder(demo)
    assert [d for d, _ in orgs] == ["NaVi", "M80"]


def test_vincere_does_not_split_on_embedded_vs():
    demo = "demos/hltv/x/natus-vincere-vs-g2-m1-mirage.dem"
    orgs = orgs_from_folder(demo)
    assert [d for d, _ in orgs] == ["NaVi", "G2"]


def test_spirit_vs_falcons_still_parses():
    demo = "demos/hltv/x/spirit-vs-falcons-m3-nuke.dem"
    orgs = orgs_from_folder(demo)
    assert [d for d, _ in orgs] == ["Spirit", "Falcons"]
