"""Tests for the FACEIT POV title generation (scripts/faceit/faceit_title.py)."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "faceit"))

import faceit_title as ft  # noqa: E402


def test_title_with_kd_and_elo():
    t = ft.build_title("donk", "Mirage", [], 5512, 3470, "34/11")
    assert t == "donk (34-11) 5512 ELO vs 3470 ELO | Mirage | FACEIT CS2 POV"


def test_title_with_kd_no_elo():
    t = ft.build_title("donk", "Mirage", [], None, None, "34/11")
    assert t == "donk (34-11) | Mirage | FACEIT CS2 POV"


def test_title_elo_kept_no_kd():
    t = ft.build_title("donk", "Mirage", [], 5512, 3470, None)
    assert t == "donk 5512 ELO vs 3470 ELO | Mirage | FACEIT CS2 POV"


def test_title_no_kd_no_elo():
    t = ft.build_title("donk", "Mirage", [], None, None, None)
    assert t == "donk | Mirage | FACEIT CS2 POV"


def test_title_length_capped():
    t = ft.build_title("averyveryveryveryveryveryveryveryveryverylongplayernickname",
                       "Mirage", [], 5512, 3470, "34/11")
    assert len(t) <= 100
