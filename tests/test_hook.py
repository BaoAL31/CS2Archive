"""Hook cold open: tier ranking, kill-anchored windows, crossfade offsets.

Pure-function tests — no CS2, no demo, no ffmpeg run.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from pov.build_hook_timeline import (  # noqa: E402
    DEFAULT_TIERS,
    MIN_ROUND_DEFAULT,
    TIER_ORDER,
    round_allowed,
    timeline_matches,
    tier_of,
)
from pov.hook_plan import plan_hook  # noqa: E402
from pov.assemble_hook import crossfade_offsets  # noqa: E402

TICKRATE = 64


def _short(**kw):
    base = {"short_type": "4k", "kill_ticks": [1000, 1200, 1400, 1600],
            "start_tick": 800, "end_tick": 1800, "pov_nick": "donk"}
    base.update(kw)
    return base


# ── tier mapping ─────────────────────────────────────────────────────────

def test_clutch_tiers_map_by_disadvantage():
    for cnt, tier in (("1v5", "clutch_1v5"), ("1v4", "clutch_1v4"),
                      ("1v3", "clutch_1v3")):
        assert tier_of(_short(short_type="clutch", clutch_initial_count=cnt)) == tier


def test_clutch_outside_1v3_plus_does_not_qualify():
    # 1v1/1v2 clutches are not hook material.
    assert tier_of(_short(short_type="clutch", clutch_initial_count="1v2")) is None


def test_five_kills_outranks_four():
    assert tier_of(_short(kill_ticks=[1, 2, 3, 4, 5])) == "5k"
    assert tier_of(_short(kill_ticks=[1, 2, 3, 4])) == "4k"


def test_punch_up_is_its_own_tier():
    assert tier_of(_short(kill_ticks=[1, 2, 3, 4], punch_up_tags=["ak"])) == "punch_up"


def test_three_kill_multikill_does_not_qualify():
    # The Shorts extractor only emits multikills at >= 4 kills; a 3k must not
    # silently become a hook.
    assert tier_of(_short(kill_ticks=[1, 2, 3])) is None


def test_perfect_shots_folds_back_into_4k_when_it_is_a_real_4k():
    # The detector EXCLUDES a 4-kill ammo-efficiency short from `4k` and emits it
    # as perfect_shots instead. Since perfect_shots is no longer a hook tier,
    # those 4Ks must still be visible under the 4k umbrella.
    assert tier_of(_short(short_type="perfect_shots", kill_ticks=[1, 2, 3, 4])) == "4k"
    # A 2-3 kill 4-tap is not hook material either way.
    assert tier_of(_short(short_type="perfect_shots", kill_ticks=[1, 2])) is None
    assert tier_of(_short(short_type="perfect_shots", kill_ticks=[1, 2, 3])) is None


def test_perfect_shots_is_not_a_tier_anymore():
    assert "perfect_shots" not in TIER_ORDER
    assert "perfect_shots" not in DEFAULT_TIERS


def test_insta_kill_replaced_perfect_shots_in_the_default_ladder():
    assert "insta_kill" in TIER_ORDER
    assert "insta_kill" in DEFAULT_TIERS


def test_clutch_attempt_is_a_tier_and_shipped_in_defaults():
    assert tier_of(_short(short_type="clutch_attempt", clutch_initial_count="1v4")) == "clutch_attempt"
    assert "clutch_attempt" in DEFAULT_TIERS


def test_default_tiers_keep_best_first_order():
    assert DEFAULT_TIERS == ",".join(TIER_ORDER)
    idx = [TIER_ORDER.index(t) for t in DEFAULT_TIERS.split(",")]
    assert idx == sorted(idx), "default tiers must stay best-first"


def test_insta_kill_candidates_are_skipped_when_tier_disabled():
    # Must not touch the demo (or the collision mesh) at all when disabled.
    from pov.build_hook_timeline import insta_kill_candidates
    enabled = [t for t in TIER_ORDER if t != "insta_kill"]
    assert insta_kill_candidates(Path("nope.dem"), "76561198386265483",
                                 "de_mirage", TICKRATE, enabled) == []


def test_insta_kill_candidates_need_a_player():
    from pov.build_hook_timeline import insta_kill_candidates
    assert insta_kill_candidates(Path("nope.dem"), "", "de_mirage",
                                 TICKRATE, list(TIER_ORDER)) == []


# ── edit plan ────────────────────────────────────────────────────────────

def test_windows_are_kill_anchored_not_whole_round():
    moment = {"start_tick": 0, "end_tick": 6400,
              "kill_ticks": [1600, 3200], "round_win_tick": 3264}
    plan = plan_hook([moment], TICKRATE, max_seconds=300)
    windows = plan[0]["windows"]
    assert len(windows) == 2, "spread kills should split into separate clips"
    for w in windows:
        assert w["end_tick"] - w["start_tick"] < 6400, "round must not be kept whole"
    # first window opens before the first kill, last window holds past the win
    assert windows[0]["start_tick"] < 1600
    assert windows[-1]["end_tick"] >= 3264


def test_payoff_is_capped_so_post_death_dead_time_is_dropped():
    # clutch_attempt: the POV player dies and the round plays on for seconds.
    # The hold must stay under MAX_PAYOFF rather than run to the round end.
    from pov.hook_plan import MAX_PAYOFF
    moment = {"start_tick": 0, "end_tick": 6400,
              "kill_ticks": [1600, 3200], "round_win_tick": 4000}
    windows = plan_hook([moment], TICKRATE, max_seconds=300)[0]["windows"]
    assert windows[-1]["end_tick"] <= 3200 + MAX_PAYOFF * TICKRATE
    assert windows[-1]["end_tick"] < 4000, "must not hold all the way to the win"


def test_burst_of_kills_merges_into_one_window():
    ticks = [1000, 1064, 1128]
    moment = {"start_tick": 800, "end_tick": 2000, "kill_ticks": ticks}
    plan = plan_hook([moment], TICKRATE, max_seconds=300)
    assert len(plan[0]["windows"]) == 1


def test_window_never_leaves_the_moment_bounds():
    moment = {"start_tick": 5000, "end_tick": 5200, "kill_ticks": [5100]}
    windows = plan_hook([moment], TICKRATE, max_seconds=300)[0]["windows"]
    assert windows[0]["start_tick"] >= 5000
    assert windows[0]["end_tick"] <= 5200


def test_budget_drops_weakest_moment_first_and_keeps_one():
    weak = {"label": "4K", "start_tick": 0, "end_tick": 6400,
            "kill_ticks": [1600, 3200, 4800, 6000]}
    strong = {"label": "1v3 CLUTCH", "start_tick": 0, "end_tick": 6400,
              "kill_ticks": [1600, 3200, 4800]}
    plan = plan_hook([weak, strong], TICKRATE, max_seconds=1.0)
    assert len(plan) == 1, "must shed moments to fit the budget"
    assert plan[0]["label"] == "1v3 CLUTCH", "the strong moment must survive"


# ── round filter ─────────────────────────────────────────────────────────

def test_round_1_is_excluded_by_default():
    # Round 1 sits ~30s into the finished video: a cold-open replay of it is
    # wasted, and round 0 is the knife/warmup round.
    assert MIN_ROUND_DEFAULT == 2
    assert round_allowed(0) is False
    assert round_allowed(1) is False
    assert round_allowed(2) is True
    assert round_allowed(15) is True


def test_round_filter_rejects_unresolvable_rounds():
    assert round_allowed(None) is False
    assert round_allowed("") is False
    assert round_allowed("nope") is False


def test_round_filter_is_configurable():
    assert round_allowed(1, min_round=1) is True
    assert round_allowed(1, min_round=5) is False
    assert round_allowed(5, min_round="5") is True


# ── stale-cache guard ────────────────────────────────────────────────────

def _params(tiers=None, min_round=2, max_moments=3, max_seconds=30.0):
    return {"params": {"tiers": tiers or list(TIER_ORDER), "min_round": min_round,
                       "max_moments": max_moments, "max_seconds": max_seconds}}


def test_cache_matches_when_filters_agree():
    assert timeline_matches(_params(), tiers=list(TIER_ORDER), min_round=2,
                            max_moments=3, max_seconds=30.0) is True


def test_cache_is_stale_when_the_round_filter_changed():
    # The bug this guards: a cached timeline built before --min-round existed
    # would keep serving round-1 moments.
    assert timeline_matches(_params(min_round=1), min_round=2) is False


def test_cache_is_stale_on_any_filter_change():
    cached = _params()
    assert timeline_matches(cached, tiers=list(TIER_ORDER)[:-1]) is False
    assert timeline_matches(cached, max_moments=5) is False
    assert timeline_matches(cached, max_seconds=12.0) is False


def test_cache_without_params_is_treated_as_stale():
    assert timeline_matches({"picked": []}, min_round=2) is False
    assert timeline_matches({"params": "nonsense"}, min_round=2) is False


# ── crossfade math ───────────────────────────────────────────────────────

def test_crossfade_offsets_account_for_accumulated_fades():
    assert crossfade_offsets([3.0, 3.0], 0.5) == [2.5]
    offs = crossfade_offsets([3.0, 3.0, 3.0], 0.5)
    assert offs == [2.5, 5.0]


def test_total_length_shrinks_by_one_fade_per_join():
    durs = [4.0, 2.0, 5.0]
    fade = 0.25
    offs = crossfade_offsets(durs, fade)
    total = offs[-1] + durs[-1]
    assert abs(total - (sum(durs) - fade * (len(durs) - 1))) < 1e-9


def test_single_clip_has_no_offsets():
    assert crossfade_offsets([2.0], 0.25) == []
