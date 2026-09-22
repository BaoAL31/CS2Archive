"""Round scorer: eco/rando gates, locks, starring."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.round_score import (  # noqa: E402
    _stars_pro,
    score_rounds,
    select_rounds,
)

PRO = "76561198000000001"
RANDO = "76561198000000009"


def _mom(mtype, rnd, pro=PRO, role="attacker", **kw):
    d = {"id": f"r{rnd}-{mtype}-1", "type": mtype, "round": rnd,
         "start_tick": 1000, "end_tick": 2000, "kill_ticks": [1500],
         "pov_candidates": [{"steam_id": pro, "role": role,
                             "is_pro": pro == PRO}],
         "primary_pro_sid": pro if pro == PRO else "", "label": mtype}
    d.update(kw)
    return d


def _atl(rounds_moms):
    rounds = [{"round": rn, "start_tick": 1000 * rn, "freeze_end_tick": None,
               "end_tick": 1000 * rn + 500, "winner_team": 2,
               "win_reason": "t_killed",
               "score_after": {"2": rn, "3": 0}, "match_point_for": None,
               "overtime": False, "closes_map": None, "alive": []}
              for rn, _ in rounds_moms]
    moms = [m for _, ms in rounds_moms for m in ms]
    return {"rounds": rounds, "moments": moms}


def test_stars_pro_requires_doer_role():
    assert _stars_pro(_mom(PRO, "x", role="attacker")) is True
    assert _stars_pro(_mom(PRO, "x", role="victim")) is False
    assert _stars_pro(_mom(PRO, "x", role="teammate")) is False
    assert _stars_pro(_mom("multikill", "x", pro=RANDO, role="attacker")) is False


def test_eco_multikill_does_not_champion():
    atl = _atl([(1, [_mom("multikill", 1, kill_count=4, tier_ok=False)]),
                (2, [_mom("opener", 2)])])
    sc = score_rounds(atl)
    assert sc[2][0] >= sc[1][0]  # opener beats disqualified eco farm


def test_locks_and_victim_moment_ignored():
    # R1/R3 lock; R2's only pro moment is a victim POV (stars farmed).
    atl = _atl([
        (1, [_mom("opener", 1)]),
        (2, [_mom("multikill", 2, pro=RANDO, role="attacker",
                         kill_count=4, tier_ok=True)]),
        (3, [_mom("trade", 3)]),
    ])
    atl["moments"][1]["pov_candidates"] = [
        {"steam_id": PRO, "role": "victim", "is_pro": True}]
    atl["moments"][1]["primary_pro_sid"] = PRO
    picked = select_rounds(atl)
    assert 1 in picked and 3 in picked  # locks hold despite weak content
