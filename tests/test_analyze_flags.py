"""Tests for the analyze risk scan (dead air, death cuts, voided attempts)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from _pathsetup import ensure
ensure()

from pipeline import flag_round_risks
from round_windows import (
    CSDM_PLAY_LEAD_TICKS,
    DEFUSE_PRE_TAIL_TICKS,
    SAVE_KEEP_TICKS,
    RoundWindow,
    plan_round_windows,
)


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


POV = "76561198000000001"
MATE = "76561198000000002"
ENEMY = "76561198000000009"
T2 = "76561198000000010"


def _save_base(*, end_reason: int, pov_side: int, last_shot: int, end: int,
               died: bool = False) -> dict:
    freeze = 11000
    start = 10000
    team_a_side = pov_side
    team_b_side = 2 if pov_side == 3 else 3
    d = {
        "teamA": {"name": "Us"},
        "teamB": {"name": "Them"},
        "players": [
            {"steamId": POV, "teamName": "Us"},
            {"steamId": MATE, "teamName": "Us"},
            {"steamId": ENEMY, "teamName": "Them"},
            {"steamId": T2, "teamName": "Them"},
        ],
        "rounds": [{
            "number": 1,
            "startTick": start,
            "endTick": end,
            "freezetimeEndTick": freeze,
            "winnerSide": 2 if pov_side == 3 else 3,
            "endReason": end_reason,
            "teamASide": team_a_side,
            "teamBSide": team_b_side,
        }],
        "kills": [],
        "shots": [{
            "roundNumber": 1, "tick": last_shot, "playerSteamId": POV,
        }],
        "damages": [],
        "grenadeDestroyed": [],
        "bombsExploded": [],
    }
    if died:
        d["kills"].append({
            "roundNumber": 1, "tick": last_shot,
            "killerName": "X", "victimName": "Us",
            "killerSteamId": "9", "victimSteamId": POV,
        })
    if end_reason == 1:
        d["bombsExploded"].append({"roundNumber": 1, "tick": end - 10})
    return d


def test_ct_bomb_explode_save_trims_idle_tail() -> None:
    last = 12000
    end = last + 40 * 64  # 40s idle
    d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=end)
    wins = plan_round_windows(d, steam_id=POV)
    assert len(wins) == 1
    assert wins[0].trimmed
    assert wins[0].start_tick == 11000 - CSDM_PLAY_LEAD_TICKS
    assert wins[0].end_tick == last + SAVE_KEEP_TICKS
    assert "save" in wins[0].reason


def test_save_idle_under_30s_unchanged() -> None:
    last = 12000
    end = last + 20 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=end)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, end)]


def test_save_not_applied_without_steam_id() -> None:
    last = 12000
    end = last + 40 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=end)
    wins = plan_round_windows(d)
    assert wins == [RoundWindow(1, 10000, end)]


def test_save_skipped_if_pov_died() -> None:
    last = 12000
    end = last + 40 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=end, died=True)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, end)]


def test_t_post_plant_win_is_not_a_save() -> None:
    last = 12000
    end = last + 40 * 64
    d = _save_base(end_reason=1, pov_side=2, last_shot=last, end=end)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, end)]


def test_t_clock_save_trims_idle_tail() -> None:
    last = 12000
    end = last + 35 * 64
    d = _save_base(end_reason=12, pov_side=2, last_shot=last, end=end)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins[0].trimmed
    assert wins[0].end_tick == last + SAVE_KEEP_TICKS
    assert wins[0].start_tick == 11000 - CSDM_PLAY_LEAD_TICKS


def test_save_keep_is_15s_for_both_30s_and_60s_idle() -> None:
    last = 12000
    for idle_s in (30, 60):
        d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=last + idle_s * 64)
        wins = plan_round_windows(d, steam_id=POV)
        assert wins[0].end_tick == last + SAVE_KEEP_TICKS, idle_s
        assert SAVE_KEEP_TICKS == 15 * 64


def test_save_uses_teammate_last_action_not_just_pov() -> None:
    pov_last = 12000
    mate_last = pov_last + 10 * 64
    end = mate_last + 40 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=pov_last, end=end)
    d["shots"].append({
        "roundNumber": 1, "tick": mate_last, "playerSteamId": MATE,
    })
    wins = plan_round_windows(d, steam_id=POV)
    assert wins[0].end_tick == mate_last + SAVE_KEEP_TICKS


def test_save_not_applied_while_teammates_still_fighting() -> None:
    pov_last = 12000
    end = pov_last + 40 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=pov_last, end=end)
    d["shots"].append({
        "roundNumber": 1, "tick": end - 5 * 64, "playerSteamId": MATE,
    })
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, end)]


def test_enemy_late_shots_do_not_block_a_team_save() -> None:
    last = 12000
    end = last + 40 * 64
    d = _save_base(end_reason=1, pov_side=3, last_shot=last, end=end)
    d["shots"].append({
        "roundNumber": 1, "tick": end - 5 * 64, "playerSteamId": ENEMY,
    })
    wins = plan_round_windows(d, steam_id=POV)
    assert wins[0].end_tick == last + SAVE_KEEP_TICKS


def _kill_t(d: dict, victim: str, tick: int) -> None:
    d["kills"].append({
        "roundNumber": 1, "tick": tick,
        "killerName": "Us", "victimName": "Them",
        "killerSteamId": POV, "victimSteamId": victim,
    })


def _defuse_base(*, plant: int, defuse: int, last_action: int,
                 t_dead: bool = True) -> dict:
    d = _save_base(end_reason=7, pov_side=3, last_shot=last_action, end=defuse)
    d["bombsPlanted"] = [{"roundNumber": 1, "tick": plant}]
    d["bombsDefused"] = [{"roundNumber": 1, "tick": defuse}]
    if t_dead:
        _kill_t(d, ENEMY, last_action)
        _kill_t(d, T2, last_action)
    else:
        _kill_t(d, ENEMY, last_action)
    return d


def test_obvious_defuse_trims_when_ts_dead_and_2s_left() -> None:
    plant = 12000
    last = plant + 5 * 64
    defuse = plant + 25 * 64  # ~16s left on a 41s bomb
    d = _defuse_base(plant=plant, defuse=defuse, last_action=last)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins[0].trimmed
    assert wins[0].end_tick == defuse - DEFUSE_PRE_TAIL_TICKS
    assert "defuse" in wins[0].reason


def test_last_second_defuse_is_kept() -> None:
    plant = 12000
    last = plant + 5 * 64
    defuse = plant + 40 * 64  # ~1s left
    d = _defuse_base(plant=plant, defuse=defuse, last_action=last)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, defuse)]


def test_defuse_not_trimmed_if_a_t_is_alive() -> None:
    plant = 12000
    last = plant + 5 * 64
    defuse = plant + 25 * 64
    d = _defuse_base(plant=plant, defuse=defuse, last_action=last, t_dead=False)
    wins = plan_round_windows(d, steam_id=POV)
    assert wins == [RoundWindow(1, 10000, defuse)]


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
    test_ct_bomb_explode_save_trims_idle_tail()
    test_save_idle_under_30s_unchanged()
    test_save_not_applied_without_steam_id()
    test_save_skipped_if_pov_died()
    test_t_post_plant_win_is_not_a_save()
    test_t_clock_save_trims_idle_tail()
    test_save_keep_is_15s_for_both_30s_and_60s_idle()
    test_save_uses_teammate_last_action_not_just_pov()
    test_save_not_applied_while_teammates_still_fighting()
    test_enemy_late_shots_do_not_block_a_team_save()
    test_obvious_defuse_trims_when_ts_dead_and_2s_left()
    test_last_second_defuse_is_kept()
    test_defuse_not_trimmed_if_a_t_is_alive()
    print("PASS")
