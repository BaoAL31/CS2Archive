"""Test priority classification from rating."""
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "create_backlog", PROJECT_ROOT / "scripts" / "create_backlog.py"
)
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def test_rating_1_5_returns_high():
    assert cb.get_priority(1.5) == "high"


def test_rating_1_54_returns_high():
    assert cb.get_priority(1.54) == "high"


def test_rating_1_6_returns_high():
    assert cb.get_priority(1.6) == "high"


def test_rating_1_49_returns_medium():
    assert cb.get_priority(1.49) == "medium"


def test_rating_1_0_returns_medium():
    assert cb.get_priority(1.0) == "medium"


def test_rating_0_99_returns_low():
    assert cb.get_priority(0.99) == "low"


def test_create_backlog_entry_creates_high_priority_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        original = cb.BACKLOG_DIR
        cb.BACKLOG_DIR = tmpdir / "backlog"

        cb.create_backlog_entry(
            match_url="https://www.hltv.org/matches/123/team1-vs-team2",
            player="TestPlayer",
            map_name="Nuke",
            rating=1.54,
            kd="20-10",
            team="Team A",
            demo_path=None,
        )

        expected_file = tmpdir / "backlog" / "high" / "testplayer-nuke.md"
        assert expected_file.exists(), f"File not created at {expected_file}"

        content = expected_file.read_text(encoding="utf-8")
        assert "TestPlayer" in content
        assert "Nuke" in content
        assert "1.54" in content
        assert "high" in content

        cb.BACKLOG_DIR = original


def test_create_backlog_entry_creates_medium_priority_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        original = cb.BACKLOG_DIR
        cb.BACKLOG_DIR = tmpdir / "backlog"

        cb.create_backlog_entry(
            match_url="https://www.hltv.org/matches/123/team1-vs-team2",
            player="MidPlayer",
            map_name="Dust2",
            rating=1.2,
            kd="15-12",
            team="Team B",
            demo_path=None,
        )

        expected_file = tmpdir / "backlog" / "medium" / "midplayer-dust2.md"
        assert expected_file.exists()

        content = expected_file.read_text(encoding="utf-8")
        assert "medium" in content

        cb.BACKLOG_DIR = original


if __name__ == "__main__":
    test_rating_1_5_returns_high()
    test_rating_1_54_returns_high()
    test_rating_1_6_returns_high()
    test_rating_1_49_returns_medium()
    test_rating_1_0_returns_medium()
    test_rating_0_99_returns_low()
    test_create_backlog_entry_creates_high_priority_file()
    test_create_backlog_entry_creates_medium_priority_file()
    print("All tests passed!")
