"""Multi-POV edit timeline: scheduler, gates, coverage, budget."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_multipov_timeline import (  # noqa: E402
    _force_coverage,
    _schedule,
    build_multipov_timeline,
    moment_value,
)

PRO = "76561198000000001"
PRO2 = "76561198000000002"
RANDO = "76561198000000009"


def _mom(mtype, rnd, start, end, primary="", **kw):
    d = {"id": f"r{rnd}-{mtype}-{start}", "type": mtype, "round": rnd,
         "start_tick": start, "end_tick": end, "kill_ticks": [],
         "primary_pro_sid": primary, "label": mtype.upper()}
    d.update(kw)
    return d


def test_moment_value_gates():
    plain = _mom("multikill", 1, 1000, 2000, PRO, kill_count=3, tier_ok=True)
    v, why = moment_value(plain, set())
    assert v == 25.0
    eco = _mom("multikill", 1, 1000, 2000, PRO, kill_count=4, tier_ok=False)
    v, _ = moment_value(eco, set())
    assert v == 0.0
    no_pro = _mom("opener", 1, 1000, 2000, "")
    v, _ = moment_value(no_pro, set())
    assert v == 0.0


def test_moment_value_rando_attacker_discount():
    farmed = _mom("multikill", 1, 1000, 2000, PRO, kill_count=4,
                  tier_ok=True, attacker_steam_id=RANDO)
    v, why = moment_value(farmed, set())
    assert v == 40.0 * 0.3
    assert any("rando" in w for w in why)


def test_moment_value_eco_taint_halves_farm_adjacent():
    tainted = {1}
    closer = _mom("closer", 1, 1000, 2000, PRO)
    v, why = moment_value(closer, tainted)
    assert v == 12.0 * 0.5
    duel = _mom("duel", 1, 1000, 2000, PRO)
    v, _ = moment_value(duel, tainted)
    assert v == 30.0  # duels keep full value


def test_schedule_picks_max_value_non_overlapping():
    moments = [
        _mom("opener", 1, 0, 100, PRO),     # v5
        _mom("multikill", 1, 50, 150, PRO, kill_count=3),  # v25, overlaps opener
        _mom("duel", 2, 200, 300, PRO),     # v30
    ]
    vals = [5.0, 25.0, 30.0]
    picked = _schedule(moments, vals)
    assert picked == [1, 2]  # 55 > 5+30=35


def test_schedule_prefers_two_small_over_one_big():
    moments = [
        _mom("duel", 1, 0, 100, PRO),       # 30
        _mom("duel", 2, 150, 250, PRO),     # 30
        _mom("clutch", 3, 50, 300, PRO),    # 50, overlaps both
    ]
    vals = [30.0, 30.0, 50.0]
    picked = _schedule(moments, vals)
    assert picked == [0, 1]  # 60 > 50


def test_force_coverage_adds_missing_pro():
    moments = [
        _mom("multikill", 1, 0, 100, PRO, kill_count=3),
        _mom("duel", 2, 200, 300, PRO),
        _mom("clutch", 3, 400, 500, PRO2),  # overlaps nothing
    ]
    vals = [25.0, 30.0, 50.0]
    picked = _schedule(moments, vals)  # all fit, all picked
    assert 2 in picked
    # now make clutch overlap duel and lower-value: PRO2 must still appear
    moments[2] = _mom("clutch", 3, 250, 350, PRO2)
    vals[2] = 20.0
    picked = _schedule(moments, vals)
    picked = _force_coverage(moments, vals, picked)
    assert any(moments[i]["primary_pro_sid"] == PRO2 for i in picked)


def test_build_end_to_end_shape():
    at = {
        "timeline_version": 2,
        "rounds": [{"round": 1, "start_tick": 0, "end_tick": 1000}],
        "moments": [
            _mom("multikill", 1, 0, 640, PRO, kill_count=3, tier_ok=True),
            _mom("opener", 1, 100, 300, ""),  # gated
        ],
    }
    edit = build_multipov_timeline(at, max_minutes=5)
    assert edit["kind"] == "multipov"
    assert edit["stats"]["picked_count"] == 1
    assert edit["stats"]["pros_covered"] == 1
    p = edit["picked"][0]
    assert p["value"] == 25.0
    assert p["primary_pro_sid"] == PRO


def test_budget_trims_lowest_value_per_second():
    at = {
        "timeline_version": 2,
        "rounds": [{"round": rn, "start_tick": 0, "end_tick": 64000}
                   for rn in range(1, 4)],
        "moments": [
            _mom("duel", 1, 0, 6400, PRO),        # 100s, v30 -> 0.3 v/s
            _mom("clutch", 2, 12800, 13440, PRO),  # 10s, v50 -> 5 v/s
            _mom("multikill", 3, 25600, 13440 + 6400, PRO,
                 kill_count=3),                    # ~107s, v25
        ],
    }
    edit = build_multipov_timeline(at, max_minutes=1)
    picked_types = [m["type"] for m in edit["picked"]]
    assert "clutch" in picked_types  # best value-per-second survives
    assert edit["stats"]["total_duration_s"] <= 60.0
