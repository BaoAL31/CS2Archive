"""Test HLTV backlog priority buckets."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


from cs2archive.pov.create_backlog import get_priority  # noqa: E402


def test_rating_1_5_returns_high():
    assert get_priority(1.5) == "high"


def test_rating_1_54_returns_high():
    assert get_priority(1.54) == "high"


def test_rating_1_6_returns_high():
    assert get_priority(1.6) == "high"


def test_rating_1_49_returns_medium():
    assert get_priority(1.49) == "medium"


def test_rating_1_0_returns_medium():
    assert get_priority(1.0) == "medium"


def test_rating_0_99_returns_low():
    assert get_priority(0.99) == "low"
