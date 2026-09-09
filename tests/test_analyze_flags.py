"""Tests for the analyze risk scan (dead air, death cuts, voided attempts)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

from pipeline import flag_round_risks
from round_windows import RoundWindow, plan_round_windows


def _data(rounds, kills):
    return {
        "rounds": [{"number": n, "startTick": a, "endTick": b}
                   for n, (a, b) in rounds.items()],
        "kills": [{"roundNumber": n, "tick": t,
                   "killerName": k, "victimName": v}
                  for n, t, k, v in kills],
    }


def test_clean_round_silent() -> None:
    d = _data({1: (10000, 18000)},
              [(1, 12000, "A", "B"), (1, 17500, "NiKo", "C")])
    assert flag_round_risks(d, {"NiKo"}) == []


def test_deadair_span_flagged() -> None:
    d = _data({13: (112434, 148220)},
              [(13, 147819, "HeavyGod", "NiKo")])
    warns = flag_round_risks(d, {"NiKo"})
    assert any("13" in w and "dead air" in w for w in warns), warns


def test_demo_start_padding_silent() -> None:
    """Round 1 spans from demo start; CSDM trims lead-in itself."""
    d = _data({1: (375, 16570)},
              [(1, 12000, "A", "B"), (1, 16500, "C", "D")])
    assert flag_round_risks(d, {"Zed"}) == []


def test_post_death_director_flagged() -> None:
    """NiKo dies 30s before the last kill -> director cut warning."""
    d = _data({15: (155815, 162315)},
              [(15, 160143, "X", "NiKo"),
               (15, 162059, "MATYS", "karrigan")])
    warns = flag_round_risks(d, {"NiKo"})
    assert any("15" in w and "director" in w for w in warns), warns


def test_short_post_death_silent() -> None:
    d = _data({10: (90000, 93000)},
              [(10, 92357, "X", "NiKo"), (10, 92432, "A", "B")])
    assert flag_round_risks(d, {"NiKo"}) == []


def test_four_second_post_death_flagged() -> None:
    d = _data({7: (60000, 72000)},
              [(7, 70445, "X", "NiKo"), (7, 70733, "A", "B")])
    warns = flag_round_risks(d, {"NiKo"})
    assert any("7" in w and "director" in w for w in warns), warns


def test_voided_attempt_flagged() -> None:
    """Restart-marker cluster + 45s gap = voided tech attempt.
    The earliest cluster is the legit round start and must NOT flag."""
    kills = [(13, 128651, f"P{i}", f"P{i}") for i in range(10)]
    kills += [(13, 140545, "NertZ", "r1nkle")]
    kills += [(13, 141678, f"Q{i}", f"Q{i}") for i in range(10)]
    kills += [(13, 147819, "HeavyGod", "NiKo")]
    d = _data({13: (112434, 148220)}, kills)
    warns = [w for w in flag_round_risks(d, {"NiKo"}) if "voided" in w]
    assert any("141678" in w for w in warns), warns
    assert not any("128651" in w for w in warns), warns


def test_boundary_markers_ignored() -> None:
    """Start/end-of-span marker clusters are normal, not voided attempts."""
    kills = [(1, 10000, f"P{i}", f"P{i}") for i in range(5)]
    kills += [(1, 15000, "A", "B")]
    d = _data({1: (10000, 18000)}, kills)
    assert flag_round_risks(d, {"Zed"}) == []


def test_other_player_death_ignored() -> None:
    d = _data({2: (20000, 30000)},
              [(2, 21000, "X", "Somebody"),
               (2, 29500, "A", "B")])
    assert flag_round_risks(d, {"NiKo"}) == []


def test_oversize_draw_is_skipped() -> None:
    d = _data({13: (100000, 140000)}, [])
    d["rounds"][0]["winnerSide"] = 0
    wins = plan_round_windows(d)
    assert len(wins) == 1
    assert wins[0].skip
    assert "no winner" in wins[0].reason


def test_oversize_winner_trims_to_freeze() -> None:
    d = _data({13: (100939, 135701)},
              [(13, 132570, "donk", "PR")])
    d["rounds"][0]["winnerSide"] = 2
    d["rounds"][0]["winnerTeamName"] = "Spirit"
    d["rounds"][0]["freezetimeEndTick"] = 131290
    wins = plan_round_windows(d)
    assert not wins[0].skip
    assert wins[0].trimmed
    assert wins[0].start_tick == 131290 - 1280
    assert wins[0].end_tick == 135701
    assert (wins[0].end_tick - wins[0].start_tick) <= 25_000


def test_oversize_winner_without_freeze_trims_to_first_kill() -> None:
    d = _data({13: (100939, 135701)},
              [(13, 132570, "donk", "PR")])
    d["rounds"][0]["winnerSide"] = 3
    wins = plan_round_windows(d)
    assert wins[0].trimmed
    assert wins[0].start_tick == 132570 - 1280
    assert not wins[0].skip


def test_normal_round_unchanged() -> None:
    d = _data({1: (10000, 18000)},
              [(1, 12000, "A", "B")])
    d["rounds"][0]["winnerSide"] = 2
    wins = plan_round_windows(d)
    assert wins == [RoundWindow(1, 10000, 18000)]


if __name__ == "__main__":
    test_clean_round_silent()
    test_deadair_span_flagged()
    test_post_death_director_flagged()
    test_short_post_death_silent()
    test_voided_attempt_flagged()
    test_boundary_markers_ignored()
    test_other_player_death_ignored()
    test_oversize_draw_is_skipped()
    test_oversize_winner_trims_to_freeze()
    test_oversize_winner_without_freeze_trims_to_first_kill()
    test_normal_round_unchanged()
    print("PASS")
