"""Hook cold open: tier ranking, kill-anchored windows, jump-cut assembly.

Pure-function tests — no CS2, no demo, no ffmpeg run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from cs2archive.pov.build_hook_timeline import (  # noqa: E402
    DEFAULT_TIERS,
    MIN_ROUND_DEFAULT,
    TIER_ORDER,
    chain_segments,
    flick_speed_bonus,
    kill_credit_index,
    moment_quality,
    peek_kill_bonus,
    punch_up_singles,
    round_allowed,
    segment_quality,
    timeline_matches,
    timeline_moment_candidates,
    tier_of,
)
from cs2archive.pov.hook_plan import plan_hook  # noqa: E402
from cs2archive.pov.assemble_hook import build_hook_filter  # noqa: E402

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


def test_slow_multikill_loses_its_tier():
    # A 4k/5k spread across the round reads as solos on the HUD feed: POV
    # killfeed rows live 7.5s (the thumbnail script's rule), so only kills
    # that flood the feed together keep the multikill tier.
    spread = [1, 200, 400, 600]  # 599 ticks ≈ 9.4s at 64tps
    assert tier_of(_short(kill_ticks=spread)) is None
    assert tier_of(_short(kill_ticks=spread + [700])) is None
    assert tier_of(_short(kill_ticks=spread, punch_up_tags=["ak"])) is None
    quick = [1000, 1100, 1200, 1300]  # 300 ticks ≈ 4.7s
    assert tier_of(_short(kill_ticks=quick)) == "4k"
    assert tier_of(_short(kill_ticks=quick, punch_up_tags=["ak"])) == "punch_up"
    assert tier_of(_short(kill_ticks=quick + [1400])) == "5k"


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
    from cs2archive.pov.build_hook_timeline import insta_kill_candidates
    enabled = [t for t in TIER_ORDER if t != "insta_kill"]
    assert insta_kill_candidates(Path("nope.dem"), "76561198386265483",
                                 "de_mirage", TICKRATE, enabled) == []


def test_insta_kill_candidates_need_a_player():
    from cs2archive.pov.build_hook_timeline import insta_kill_candidates
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
    from cs2archive.pov.hook_plan import MAX_PAYOFF
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


# ── jump-cut assembly ────────────────────────────────────────────────

def test_moments_join_with_concat_not_xfade():
    fc = build_hook_filter([0, 2], [0, 2], end_fade=0.5, total=6.0,
                           width=2560, height=1440, fps=60.0)
    assert "concat=n=2:v=1:a=1" in fc
    assert "[v0][a0][v1][a1]concat" in fc
    assert "xfade" not in fc
    assert "acrossfade" not in fc


def test_tail_fades_to_black_for_the_intro_dip():
    fc = build_hook_filter([0], [0], end_fade=0.5, total=4.0,
                           width=2560, height=1440, fps=60.0)
    assert "fade=t=out:st=3.500:d=0.5" in fc
    assert "afade=t=out:st=3.500:d=0.5" in fc


def test_zero_end_fade_means_hard_cut_to_black_free():
    fc = build_hook_filter([0, 1], [0, 1], end_fade=0.0, total=6.0,
                           width=2560, height=1440, fps=60.0)
    assert "concat=n=2:v=1:a=1" in fc
    assert "fade=t=out" not in fc
    assert "afade=t=out" not in fc


def test_audio_filler_input_is_addressable():
    # Second clip has no audio: its filler lands on input 2.
    fc = build_hook_filter([0, 1], [0, 2], end_fade=0.5, total=6.0,
                           width=2560, height=1440, fps=60.0)
    assert "[2:a]" in fc
    assert "concat=n=2:v=1:a=1" in fc


# ── single-kill window cap (no 2k from an insta moment) ───────────────

def _insta(**kw):
    base = {"start_tick": 72987, "end_tick": 73275, "kill_ticks": [73147],
            "round_win_tick": None, "label": "INSTA", "tier": "insta_kill",
            "pov_steam_id": "x", "round": 14}
    base.update(kw)
    return base


def test_single_kill_window_ends_before_next_kill():
    # kyousuke reality: insta HS at 73147, next kill 96 ticks later. The
    # payoff must not stretch into it or the cold open reads as a 2k.
    wins = plan_hook([_insta()], TICKRATE,
                     all_kill_ticks=[69887, 73147, 73243])[0]["windows"]
    assert wins[0]["end_tick"] < 73243
    assert wins[0]["end_tick"] >= 73147 + 48  # kill + beat always play
    assert wins[0]["start_tick"] == 73083  # 1s pre-kill setup


def test_far_next_kill_leaves_window_unchanged():
    plain = plan_hook([_insta()], TICKRATE)[0]["windows"]
    capped = plan_hook([_insta()], TICKRATE,
                       all_kill_ticks=[73147, 90000])[0]["windows"]
    assert capped == plain


def test_multikill_window_ignores_the_cap():
    m = _insta(kill_ticks=[73147, 73243])
    wins = plan_hook([m], TICKRATE,
                     all_kill_ticks=[69887, 73147, 73243])[0]["windows"]
    assert wins[0]["end_tick"] == 73275


def test_cap_floor_keeps_the_kill_beat_on_instant_trades():
    # Next kill 0.3s later: cap would land before the kill itself — the floor
    # (kill + 0.75s) wins instead of a degenerate window.
    wins = plan_hook([_insta()], TICKRATE,
                     all_kill_ticks=[73147, 73166])[0]["windows"]
    assert wins[0]["end_tick"] >= 73147 + 48


# ── minimum hook length (no disorienting 3s flash) ───────────────────

def test_no_kill_list_means_no_cap():
    assert plan_hook([_insta()], TICKRATE)[0]["windows"] == \
        plan_hook([_insta()], TICKRATE, all_kill_ticks=None)[0]["windows"]


def test_planned_seconds_reports_assembled_footage():
    from cs2archive.pov.hook_plan import planned_seconds
    plan = plan_hook([_insta()], TICKRATE,
                     all_kill_ticks=[69887, 73147, 73243])
    total = planned_seconds(plan, TICKRATE)
    assert total == 2.0  # [kill-64, kill+64]: 1s setup + 1s hold
    assert total < 10.0, "a lone capped insta kill must fall below the floor"


def test_two_short_moments_still_fall_below_the_floor():
    from cs2archive.pov.hook_plan import planned_seconds
    # Two single kills ~1.5s apart: capped first window (2s) + full second
    # (2s) stays below the floor — ships no hook.
    kills = [73147, 73243]
    moments = [_insta(start_tick=k - 200, end_tick=k + 200, kill_ticks=[k])
               for k in kills]
    plan = plan_hook(moments, TICKRATE, all_kill_ticks=kills)
    assert planned_seconds(plan, TICKRATE) < 10.0


def test_clustered_singles_can_clear_the_bar_together():
    from cs2archive.pov.hook_plan import planned_seconds
    # Eight single kills ~1.5s apart: 8 x 2s of action across the jump cuts
    # clears the 15s bar — a real cold open, not a lone flash.
    kills = [73147 + i * 96 for i in range(8)]
    moments = [_insta(start_tick=k - 200, end_tick=k + 200, kill_ticks=[k])
               for k in kills]
    plan = plan_hook(moments, TICKRATE, all_kill_ticks=kills)
    assert planned_seconds(plan, TICKRATE) >= 15.0


def test_clutch_moment_plans_three_windows():
    from cs2archive.pov.hook_plan import planned_seconds
    m = _insta(start_tick=0, end_tick=6400, kill_ticks=[1600, 3200, 4800],
               round_win_tick=5000)
    plan = plan_hook([m], TICKRATE)
    assert planned_seconds(plan, TICKRATE) == 6.0  # 3 x 128 ticks


# ── insta pairing (double-insta or flick solo, never lone) ────────────

def _irow(tick, rnd=11, reasons=None, hp=100.0, vic=None, ttk=0.25, head="head"):
    return {"kill_tick": tick, "round": rnd,
            "reasons": reasons or ["insta_kill"],
            "los_open_tick": tick - int(ttk * 64),
            "attacker_sid": "x", "victim_sid": vic or f"v{tick}",
            "weapon": "ak47", "hitgroup": head, "victim_hp": hp}


def _pair(rows):
    from cs2archive.pov.build_hook_timeline import pair_insta_rows
    enabled = list(TIER_ORDER)
    return pair_insta_rows(rows, "x", enabled, TICKRATE)


def test_lone_nonflick_insta_qualifies_as_solo():
    out = _pair([_irow(61341)])
    assert len(out) == 1
    assert out[0]["kill_ticks"] == [61341]
    assert out[0]["tier"] == "insta_kill"
    assert not out[0].get("chained")
    assert out[0]["label"].startswith("INSTA")
    # ...however clean the victim, and whatever the HP.
    out = _pair([_irow(61341, hp=16.0)])
    assert len(out) == 1


def test_flick_solo_stays_eligible():
    out = _pair([_irow(73147, rnd=14, reasons=["flick", "insta_kill"])])
    assert len(out) == 1
    assert out[0]["kill_ticks"] == [73147]
    assert out[0]["label"].startswith("FLICK")
    assert not out[0].get("chained")


def test_clean_pair_within_3s_chains():
    out = _pair([_irow(61341, hp=100.0), _irow(61460, hp=20.0)])
    assert len(out) == 1
    assert out[0]["kill_ticks"] == [61341, 61460]
    assert out[0].get("chained") is True
    assert out[0]["label"].startswith("2× INSTA")


def test_tagged_pair_fails_the_clean_gate_but_solos_survive():
    # Both victims damaged: no chained pair — but each lone kill still
    # ships as a solo moment.
    out = _pair([_irow(61341, hp=16.0), _irow(61460, hp=20.0)])
    assert [m["kill_ticks"] for m in out] == [[61341], [61460]]
    assert all(not m.get("chained") for m in out)


def test_pair_splits_beyond_3s_into_solos():
    out = _pair([_irow(1000, rnd=2), _irow(1400, rnd=2)])
    assert [m["kill_ticks"] for m in out] == [[1000], [1400]]


def test_pair_requires_same_round_and_distinct_victims():
    # ...to CHAIN. Unchainable kills still qualify alone.
    out = _pair([_irow(1000, rnd=2), _irow(1100, rnd=3)])
    assert [m["kill_ticks"] for m in out] == [[1000], [1100]]
    out = _pair([_irow(1000, vic="same"), _irow(1100, vic="same")])
    assert [m["kill_ticks"] for m in out] == [[1000], [1100]]


def test_missing_hp_blocks_pair_but_not_solos():
    out = _pair([_irow(61341, hp=None), _irow(61460, hp=None)])
    assert [m["kill_ticks"] for m in out] == [[61341], [61460]]


def test_footage_budget_measures_planned_not_uncut():
    from cs2archive.pov.build_hook_timeline import enforce_footage_budget
    # 56s uncut detection span shipping one 2s window must not blow the budget.
    wide = _cand(1000, tier_rank=8, start=0, end=3600)
    wide["quality"] = 4000.0
    tight = _cand(5000, tier_rank=6, start=4900, end=5200)
    tight["quality"] = 6000.0
    kept = enforce_footage_budget([wide, tight], TICKRATE, 60.0)
    assert kept == [wide, tight]


def test_footage_budget_drops_weakest_first():
    from cs2archive.pov.build_hook_timeline import enforce_footage_budget
    weak = _cand(1000, tier_rank=8, start=0, end=3600)
    weak["quality"] = 4000.0
    strong = _cand(5000, tier_rank=6, start=4900, end=5200)
    strong["quality"] = 6000.0
    assert enforce_footage_budget([weak, strong], TICKRATE, 3.0) == [strong]
    # Never drops the last chain, however far over budget.
    assert enforce_footage_budget([weak], TICKRATE, 0.5) == [weak]


def test_rule_version_bumps_cache():
    from cs2archive.pov.build_hook_timeline import INSTA_RULE_VERSION, timeline_matches
    assert INSTA_RULE_VERSION == 9
    assert timeline_matches({"params": {"tiers": [], "min_round": 2,
                                        "max_moments": 3, "max_seconds": 30.0,
                                        "rule_version": 7}},
                            tiers=[], min_round=2, max_moments=3,
                            max_seconds=30.0, rule_version=7) is True
    assert timeline_matches({"params": {"tiers": [], "min_round": 2,
                                        "max_moments": 3, "max_seconds": 30.0}},
                            tiers=[], min_round=2, max_moments=3,
                            max_seconds=30.0, rule_version=7) is False


# ── canonical nick (same crosshair input as the POV) ──────────────────

def test_canonical_nick_resolves_by_steam_id(tmp_path):
    from cs2archive.crosshair_resolve import canonical_nick
    acc = tmp_path / "player_accounts.json"
    acc.write_text(__import__("json").dumps([
        {"steam_id": "76561199032006224", "nickname": "kyousuke"},
    ]))
    assert canonical_nick("76561199032006224", "76561199032006224",
                          accounts_path=acc) == "kyousuke"
    assert canonical_nick("000", "rawname", accounts_path=acc) == "rawname"


# ── chaining (<5s merges, cut at/above) ────────────────────────────────

def _cand(kill, tier_rank=6, start=None, end=None, **kw):
    base = {"start_tick": kill - 160, "end_tick": kill + 128,
            "kill_ticks": [kill], "tier": "insta_kill", "tier_rank": tier_rank,
            "label": f"INSTA {kill}", "rank_reason": "r", "round": 11,
            "pov_steam_id": "x", "pov_nick": "x"}
    if start is not None:
        base["start_tick"] = start
    if end is not None:
        base["end_tick"] = end
    base.update(kw)
    return base


def test_close_moments_chain_into_one():
    from cs2archive.pov.build_hook_timeline import pick_moments
    # kyousuke 7:44 chain: kills 1.9s apart must not jump-cut.
    picked = pick_moments([_cand(61341), _cand(61460)], TICKRATE, 3)
    assert len(picked) == 1
    m = picked[0]
    assert m.get("chained") is True
    assert m["kill_ticks"] == [61341, 61460]
    assert m["start_tick"] == 61341 - 160
    assert m["end_tick"] == 61460 + 128


def test_best_tier_wins_the_chain():
    from cs2archive.pov.build_hook_timeline import pick_moments
    weak = _cand(61341, tier_rank=6, label="INSTA")
    strong = _cand(61460, tier_rank=3, label="4K")
    picked = pick_moments([weak, strong], TICKRATE, 3)
    assert len(picked) == 1
    assert picked[0]["tier_rank"] == 3
    assert picked[0]["label"] == "4K"
    assert picked[0]["kill_ticks"] == [61341, 61460]


def test_distant_moments_stay_separate_clips():
    from cs2archive.pov.build_hook_timeline import pick_moments
    # 6s kill gap: cut, two moments.
    picked = pick_moments([_cand(61341), _cand(61341 + 384)], TICKRATE, 3)
    assert len(picked) == 2
    assert all(not m.get("chained") for m in picked)


def test_chain_counts_as_one_toward_max_moments():
    from cs2archive.pov.build_hook_timeline import pick_moments
    cands = [_cand(61341), _cand(61460), _cand(70000)]
    picked = pick_moments(cands, TICKRATE, 2)
    assert len(picked) == 2
    assert picked[0]["kill_ticks"] == [61341, 61460]


def test_span_cap_refuses_runaway_chains():
    from cs2archive.pov.build_hook_timeline import pick_moments
    # Overlapping bounds but a union beyond the 15s span cap: dropped, not merged.
    wide = _cand(61341, start=61341 - 800, end=61341 + 800)
    other = _cand(61460, start=61460 - 800, end=61460 + 800)
    picked = pick_moments([wide, other], TICKRATE, 3)
    assert len(picked) == 1
    assert not picked[0].get("chained")


def test_chained_moment_plans_one_continuous_window():
    # Same kills 3s apart: chained -> one window (no cut); unchained -> two.
    m = _insta(start_tick=61000, end_tick=62000, kill_ticks=[61341, 61533],
               chained=True)
    wins = plan_hook([m], TICKRATE)[0]["windows"]
    assert len(wins) == 1
    assert wins[0]["start_tick"] <= 61341 - 64
    assert wins[0]["end_tick"] >= 61533
    m2 = _insta(start_tick=61000, end_tick=62000, kill_ticks=[61341, 61533])
    assert len(plan_hook([m2], TICKRATE)[0]["windows"]) == 2


def test_multikill_payoff_capped_at_next_kill():
    # A 4k tail running into the next POV kill reads as a 5k — cap it.
    m = _insta(start_tick=60000, end_tick=64000,
               kill_ticks=[61000, 61100, 61200, 61300])
    wins = plan_hook([m], TICKRATE,
                     all_kill_ticks=[61000, 61100, 61200, 61300, 61400])[0]["windows"]
    assert wins[-1]["end_tick"] < 61400
    assert wins[-1]["end_tick"] >= 61300 + 48
    # Far next kill: the tail rests on the 1s payoff instead of the cap.
    wins2 = plan_hook([m], TICKRATE,
                      all_kill_ticks=[61000, 61100, 61200, 61300, 70000])[0]["windows"]
    assert wins2[-1]["end_tick"] == 61300 + 64


def test_bridge_candidate_joins_chain_despite_full_quota():
    # max_moments=1: the first candidate takes the only slot, but a later
    # chainable candidate still merges (merges never consume slots).
    from cs2archive.pov.build_hook_timeline import pick_moments
    cands = [_cand(61341), _cand(61460), _cand(61550)]
    picked = pick_moments(cands, TICKRATE, 1)
    assert len(picked) == 1
    assert picked[0]["kill_ticks"] == [61341, 61460, 61550]
    assert picked[0].get("chained") is True


# ── action-timeline tiers (punch-up singles + duel/opener/trade) ───

def test_new_tiers_ship_in_defaults_in_rank_order():
    for t in ("punch_up_single", "duel", "opener", "trade"):
        assert t in TIER_ORDER
        assert t in DEFAULT_TIERS
    idx = TIER_ORDER.index
    assert idx("4k") < idx("punch_up_single") < idx("insta_kill")
    assert idx("insta_kill") < idx("duel") < idx("clutch_attempt")
    assert idx("clutch_attempt") < idx("opener") < idx("trade")


def _prow(tick, rnd=2, reasons=None, ttk=0.12):
    return {"kill_tick": tick, "round": rnd,
            "reasons": reasons or ["insta_kill"],
            "los_open_tick": tick - int(ttk * 64),
            "attacker_sid": "x", "victim_sid": f"v{tick}",
            "weapon": "deagle", "hitgroup": "head", "victim_hp": 100.0}


def _tkill(tick, rnd=2, aid="x", weapon="deagle", victim_weapon="AK-47",
           hs=True):
    return {"tick": tick, "round": rnd, "attacker_steam_id": aid,
            "victim_steam_id": "v", "weapon": weapon,
            "victim_weapon": victim_weapon, "headshot": hs}


def _penabled():
    return list(TIER_ORDER)


def test_punch_up_single_deagle_vs_rifle_head():
    out = punch_up_singles([_prow(11582)], [_tkill(11582)], "x",
                           _penabled(), TICKRATE)
    assert len(out) == 1
    m = out[0]
    assert m["tier"] == "punch_up_single"
    assert m["kill_ticks"] == [11582]
    assert m["label"].startswith("DEAGLE PUNCH-UP")
    assert m["round"] == 2
    assert m["hs_ticks"] == {11582: 50.0}
    assert m["hs_bonus"] == 50.0


def test_punch_up_single_rejects_non_punch_up():
    rows = [_prow(11582)]
    assert punch_up_singles(rows, [_tkill(11582, hs=False)], "x",
                            _penabled(), TICKRATE) == []
    assert punch_up_singles(rows, [_tkill(11582, weapon="ak47")], "x",
                            _penabled(), TICKRATE) == []
    assert punch_up_singles(rows, [_tkill(11582, victim_weapon="MP9")], "x",
                            _penabled(), TICKRATE) == []
    assert punch_up_singles(rows, [_tkill(11582, aid="y")], "x",
                            _penabled(), TICKRATE) == []
    assert punch_up_singles(rows, [], "x",
                            _penabled(), TICKRATE) == []


def test_punch_up_single_needs_no_rewind_row():
    # First contact is scoring input, not eligibility: a Deagle headshot
    # onto a rifle with no measured LOS (tracked re-kill, missing snaps)
    # still qualifies — it just carries no TTK.
    out = punch_up_singles([], [_tkill(112854)], "x", _penabled(), TICKRATE)
    assert len(out) == 1
    assert out[0]["kill_ticks"] == [112854]
    assert out[0]["ttk"] is None
    assert out[0]["label"] == "DEAGLE PUNCH-UP"


def test_punch_up_singles_sort_fastest_first():
    out = punch_up_singles([_prow(200, ttk=0.2), _prow(100, ttk=0.1)],
                           [_tkill(200), _tkill(100)], "x",
                           _penabled(), TICKRATE)
    assert [m["kill_ticks"] for m in out] == [[100], [200]]


def test_punch_up_single_accepts_slow_peek():
    # 0.44s first contact: over the 0.3s insta cap (no insta stacking) but
    # inside the 0.5s measured-contact window — still a punch-up single.
    out = punch_up_singles([_prow(112737, ttk=0.44, reasons=["peek"])],
                           [_tkill(112737)], "x", _penabled(), TICKRATE)
    assert len(out) == 1
    assert out[0]["tier"] == "punch_up_single"
    assert out[0]["ttk"] == 28 / 64


def _amoment(mid, mtype, rnd=2, kills=(11582,), start=11262, end=11710):
    return {"id": mid, "type": mtype, "round": rnd,
            "start_tick": start, "end_tick": end,
            "kill_ticks": list(kills)}


def test_moment_candidates_take_duel_opener_trade():
    moments = [_amoment("r2-opener-1", "opener"),
               _amoment("r2-trade-4", "trade"),
               _amoment("r11-duel-2", "duel", rnd=11,
                        kills=(63864, 65572), start=63000, end=66800,
                        )]
    out = timeline_moment_candidates(moments, "x", _penabled())
    assert [(m["tier"], m["round"]) for m in out] == [
        ("opener", 2), ("trade", 2), ("duel", 11)]
    assert out[0]["kill_ticks"] == [11582]
    assert out[2]["label"] == "DUEL"


def test_moment_candidates_skip_bomb_util_and_empty():
    moments = [_amoment("r20-bomb-1", "bomb_plant", kills=()),
               _amoment("r3-burst-1", "util_burst", kills=()),
               _amoment("r5-opener-1", "opener", kills=(29522,))]
    out = timeline_moment_candidates(moments, "x", _penabled())
    assert [m["tier"] for m in out] == ["opener"]


def test_moment_candidates_respect_enabled_tiers():
    moments = [_amoment("r2-opener-1", "opener"),
               _amoment("r2-trade-4", "trade")]
    enabled = [t for t in TIER_ORDER if t != "trade"]
    assert [m["tier"] for m in timeline_moment_candidates(
        moments, "x", enabled)] == ["opener"]


def _tkill_row(tick, sid="x"):
    return {"tick": tick, "attacker_steam_id": sid}


def test_moment_candidates_keep_pov_kills_only():
    # r9-duel-3 carries two T-on-T teamkills around the POV's own kill.
    moments = [_amoment("r9-duel-3", "duel", rnd=9,
                        kills=(68096, 68429, 68435), start=67776, end=68563)]
    out = timeline_moment_candidates(
        moments, "x", _penabled(), timeline_kills=[_tkill_row(68435)])
    assert [m["kill_ticks"] for m in out] == [[68435]]


def test_moment_candidates_drop_all_teammate_moments():
    moments = [_amoment("r9-duel-3", "duel", rnd=9,
                        kills=(68096, 68429), start=67776, end=68563)]
    out = timeline_moment_candidates(
        moments, "x", _penabled(), timeline_kills=[_tkill_row(68435)])
    assert out == []


def test_moment_candidates_unfiltered_without_timeline_kills():
    moments = [_amoment("r9-duel-3", "duel", rnd=9,
                        kills=(68096, 68429, 68435), start=67776, end=68563)]
    out = timeline_moment_candidates(moments, "x", _penabled())
    assert [m["kill_ticks"] for m in out] == [[68096, 68429, 68435]]


# ── headshot bonus (every headshot the same, bodies nothing) ──────

def test_headshot_bonus_flat_across_guns():
    from cs2archive.weapons import headshot_bonus
    assert headshot_bonus("m4a1_silencer") == 50.0
    assert headshot_bonus("M4A4") == 50.0
    assert headshot_bonus("ak47") == 50.0
    assert headshot_bonus("AK-47") == 50.0
    assert headshot_bonus("deagle") == 50.0
    assert headshot_bonus("awp") == 50.0
    assert headshot_bonus("hegrenade") == 0.0
    assert headshot_bonus("knife") == 0.0
    assert headshot_bonus("") == 0.0


def test_insta_solo_carries_hs_bonus():
    out = _pair([_irow(42274, hp=100.0)])
    assert out[0]["hs_bonus"] == 50.0  # ak47 head in _irow


def test_insta_pair_sums_hs_bonus():
    out = _pair([_irow(61341, hp=100.0), _irow(61460, hp=20.0)])
    assert len(out) == 1
    assert out[0]["hs_bonus"] == 100.0  # 2x ak47 head


def test_body_shot_scores_no_hs_bonus():
    out = _pair([_irow(61341, hp=100.0, head="chest")])
    assert out[0]["hs_bonus"] == 0.0
    assert "HEAD" not in out[0]["label"]


def test_hs_bonus_never_breaks_tier_order():
    weak_punch = {"tier": "punch_up_single",
                  "tier_rank": TIER_ORDER.index("punch_up_single"),
                  "kill_ticks": [1], "ttk": 0.5,
                  "clutch_initial_count": "", "start_tick": 1,
                  "hs_bonus": 0.0}
    rich_insta = {"tier": "insta_kill",
                  "tier_rank": TIER_ORDER.index("insta_kill"),
                  "kill_ticks": [2], "ttk": 0.06,
                  "clutch_initial_count": "", "start_tick": 2,
                  "hs_bonus": 75.0}
    assert moment_quality(weak_punch) > moment_quality(rich_insta)


def test_trade_deters_insta_scale_points():
    from cs2archive.pov.build_hook_timeline import TRADE_DETER_POINTS
    assert TRADE_DETER_POINTS == 100.0  # ceiling of the (0.5 - ttk) * 200 scale
    clean = _cand(68435, tier_rank=8)
    traded = _cand(68435, tier_rank=8, trade_ticks=[68435])
    assert moment_quality(clean) - moment_quality(traded) == 100.0


def test_trade_deter_survives_chain_merge():
    from cs2archive.pov.build_hook_timeline import _merge_moments
    target = _cand(68435, tier_rank=8, start=67776, end=68563)
    src = _cand(68435, tier_rank=11, start=68115, end=68563,
                trade_ticks=[68435])
    _merge_moments(target, src)
    assert target["trade_ticks"] == [68435]
    assert target["tier_rank"] == 8  # best tier still wins
    assert moment_quality(_cand(68435, tier_rank=8)) - moment_quality(target) == 100.0


def test_moment_candidates_score_heads_up():
    kills = [{"tick": 29522, "attacker_steam_id": "x", "weapon": "m4a1_silencer",
              "headshot": True},
             {"tick": 39485, "attacker_steam_id": "x", "weapon": "ak47",
              "headshot": False}]
    moments = [_amoment("r5-opener-1", "opener", rnd=5, kills=(29522,)),
               _amoment("r7-opener-2", "opener", rnd=7, kills=(39485,),
                        start=39165, end=39613)]
    out = timeline_moment_candidates(moments, "x", _penabled(),
                                     timeline_kills=kills)
    assert [m["hs_bonus"] for m in out] == [50.0, 0.0]
    assert moment_quality(out[0]) > moment_quality(out[1])


def test_merged_chain_stacks_hs_bonus():
    # 2 headshots fused into one segment must outscore either alone.
    from cs2archive.pov.build_hook_timeline import pick_moments
    a = _cand(61341, tier_rank=7, label="INSTA A",
              start=61341 - 160, end=61341 + 128)
    a["hs_ticks"] = {61341: 50.0}
    a["hs_bonus"] = 50.0
    b = _cand(61460, tier_rank=7, label="INSTA B",
              start=61460 - 160, end=61460 + 128)
    b["hs_ticks"] = {61460: 50.0}
    b["hs_bonus"] = 50.0
    solo = _cand(70000, tier_rank=7, label="INSTA C",
                 start=70000 - 160, end=70000 + 128)
    solo["hs_ticks"] = {70000: 50.0}
    solo["hs_bonus"] = 50.0
    picked = pick_moments([a, b, solo], TICKRATE, 3)
    assert len(picked) == 2
    chain = next(m for m in picked if m.get("chained"))
    assert chain["kill_ticks"] == [61341, 61460]
    assert chain["hs_bonus"] == 100.0
    assert chain["hs_ticks"] == {61341: 50.0, 61460: 50.0}
    assert moment_quality(chain) > moment_quality(solo)


def test_merge_counts_shared_kill_once():
    # Same headshot referenced by two fused candidates (insta solo +
    # opener on the same tick) scores once, not twice.
    from cs2archive.pov.build_hook_timeline import pick_moments
    a = _cand(61341, tier_rank=6, label="PUNCH",
              start=61341 - 160, end=61341 + 128)
    a["hs_ticks"] = {61341: 50.0}
    a["hs_bonus"] = 50.0
    b = _cand(61341, tier_rank=7, label="INSTA",
              start=61341 - 160, end=61341 + 128)
    b["hs_ticks"] = {61341: 50.0}
    b["hs_bonus"] = 50.0
    picked = pick_moments([a, b], TICKRATE, 3)
    assert len(picked) == 1
    assert picked[0]["hs_bonus"] == 50.0
    assert picked[0]["hs_ticks"] == {61341: 50.0}


def test_punch_chain_counts_each_head_once():
    # Three punch-up headshots chained: 150, beating any single-headshot
    # moment at the same tier.
    from cs2archive.pov.build_hook_timeline import pick_moments
    cands = []
    for i, tick in enumerate([112654, 112737, 112854]):
        m = _cand(tick, tier_rank=6, label="PUNCH",
                  start=tick - 160, end=tick + 128)
        m["hs_ticks"] = {tick: 50.0}
        m["hs_bonus"] = 50.0
        cands.append(m)
    picked = pick_moments(cands, TICKRATE, 3)
    assert len(picked) == 1
    assert picked[0]["hs_bonus"] == 150.0
    assert moment_quality(picked[0]) > moment_quality(cands[0])


# ── peek-kill bonus (taper) + flick-speed bonus ───────────────────

def test_peek_bonus_taper():
    from cs2archive.pov.build_hook_timeline import peek_kill_bonus
    assert peek_kill_bonus(0.8) == 100.0   # dead-on hold: full credit
    assert peek_kill_bonus(15.0) == 100.0
    assert peek_kill_bonus(30.0) == 50.0   # ambiguous lane: half
    assert peek_kill_bonus(32.2) == pytest.approx(42.7, abs=0.1)
    assert peek_kill_bonus(40.0) == pytest.approx(16.7, abs=0.1)
    assert peek_kill_bonus(45.0) == 0.0    # back/side shot: nothing
    assert peek_kill_bonus(82.3) == 0.0
    assert peek_kill_bonus(None) == 0.0    # unknown: fail closed
    assert peek_kill_bonus(999.0) == 0.0   # degenerate-geometry sentinel


def test_flick_speed_bonus_clamp():
    assert flick_speed_bonus(58) == 0.0     # ordinary tracking
    assert flick_speed_bonus(100) == 0.0    # floor
    assert flick_speed_bonus(138) == pytest.approx(28.5)
    assert flick_speed_bonus(204) == pytest.approx(78.0)
    assert flick_speed_bonus(453) == 150.0  # cap
    assert flick_speed_bonus(None) == 0.0


def test_peek_ak_head_beats_m4_back_shot():
    # The acceptance target: 135419 (AK peek head, ttk 0.25, hold 0.8)
    # must outscore 45079 (M4 back shot, ttk 0.19, hold 82.3) even though
    # the back shot has the faster TTK.
    peek_ak = _q("insta_kill", ttk=0.25)
    peek_ak.update({"hs_bonus": 50.0,
                    "peek_ticks": {1000: 100.0},
                    "flick_speed": 58.0})
    back_m4 = _q("insta_kill", ttk=0.19)
    back_m4.update({"hs_bonus": 50.0,
                    "peek_ticks": {},
                    "flick_speed": 27.0})
    assert moment_quality(peek_ak) > moment_quality(back_m4)


def test_full_peek_head_outscores_partial_peek_body_flick():
    # The 65572 absurdity guard: a 10hp body-shot flick into a 32 deg hold
    # must not outrank a full-HP AK head peek.
    flick_body = _q("insta_kill", ttk=0.19)
    flick_body.update({"hs_bonus": 0.0,
                       "peek_ticks": {1000: peek_kill_bonus(32.2)},
                       "flick_speed": 204.0})
    peek_ak = _q("insta_kill", ttk=0.31)
    peek_ak.update({"hs_bonus": 50.0,
                    "peek_ticks": {1000: 100.0},
                    "flick_speed": 67.0})
    assert moment_quality(peek_ak) > moment_quality(flick_body)


def test_merge_unions_peek_and_maxes_flick():
    from cs2archive.pov.build_hook_timeline import pick_moments
    a = _cand(61341, tier_rank=7, label="A",
              start=61341 - 160, end=61341 + 128)
    a["hs_ticks"] = {}; a["hs_bonus"] = 0.0
    a["peek_ticks"] = {61341: 100.0}; a["flick_speed"] = 120.0
    b = _cand(61460, tier_rank=7, label="B",
              start=61460 - 160, end=61460 + 128)
    b["hs_ticks"] = {}; b["hs_bonus"] = 0.0
    b["peek_ticks"] = {61460: 50.0}; b["flick_speed"] = 260.0
    picked = pick_moments([a, b], TICKRATE, 3)
    assert len(picked) == 1
    assert picked[0]["peek_ticks"] == {61341: 100.0, 61460: 50.0}
    assert picked[0]["flick_speed"] == 260.0  # max, never summed
    # Shared-kill fusion counts the peek once, not twice.
    c = _cand(61341, tier_rank=6, label="C",
              start=61341 - 160, end=61341 + 128)
    c["hs_ticks"] = {}; c["hs_bonus"] = 0.0
    c["peek_ticks"] = {61341: 100.0}; c["flick_speed"] = None
    picked2 = pick_moments([a, c], TICKRATE, 3)
    assert len(picked2) == 1
    assert picked2[0]["peek_ticks"] == {61341: 100.0}


# ── quality levels (least impressive first) ────────────────────────

def _q(tier, kills=1, ttk=None, clutch="", start=1000):
    return {"tier": tier, "tier_rank": TIER_ORDER.index(tier),
            "kill_ticks": [start + i for i in range(kills)],
            "ttk": ttk, "clutch_initial_count": clutch,
            "start_tick": start}


def test_quality_tier_dominates_kills_and_ttk():
    assert moment_quality(_q("4k", kills=4)) > moment_quality(
        _q("punch_up_single", kills=1, ttk=0.06))
    assert moment_quality(_q("punch_up_single", kills=1)) > moment_quality(
        _q("insta_kill", kills=3, ttk=0.06))


def test_quality_more_kills_then_faster_ttk():
    one = _q("punch_up_single", kills=1, ttk=0.12)
    two = _q("punch_up_single", kills=2, ttk=0.44)
    assert moment_quality(two) > moment_quality(one)
    fast = _q("insta_kill", kills=2, ttk=0.1)
    slow = _q("insta_kill", kills=2, ttk=0.4)
    assert moment_quality(fast) > moment_quality(slow)


def test_quality_no_clutch_disadvantage_bonus():
    # The tier base already pays clutches top dollar; the count string
    # itself must not add anything (1v5 and 1v3 are different tiers, so
    # compare within one tier).
    counted = _q("clutch_1v4", kills=3, clutch="1v4")
    uncounted = _q("clutch_1v4", kills=3, clutch="")
    assert moment_quality(counted) == moment_quality(uncounted)


# ── segment scoring (the unit watched, not the unit detected) ──────

def _seg_moment(tier, kills, start=1000, **kw):
    m = {"tier": tier, "tier_rank": TIER_ORDER.index(tier),
         "kill_ticks": list(kills), "start_tick": start,
         "end_tick": start + 5000, "hs_ticks": {}, "peek_ticks": {},
         "trade_ticks": [], "flick_speed": None, "ttk": None,
         "clutch_initial_count": ""}
    m.update(kw)
    return m


def _win(a, b):
    return {"start_tick": a, "end_tick": b}


def test_segment_whole_window_keeps_moment_score():
    m = _seg_moment("punch_up_single", [1000, 1100, 1200],
                    hs_ticks={1000: 50.0})
    credit = kill_credit_index(None, None, [m])
    assert (segment_quality([1000, 1100, 1200], m, credit)
            == moment_quality(m))


def test_segment_fragment_scores_enclosed_kills_only():
    m = _seg_moment("clutch_1v4", [1000, 2000, 3000, 4000])
    credit = {1000: {"hs": True, "peek": 100.0, "trade": False},
              2000: {"hs": False, "peek": 0.0, "trade": True},
              3000: {"hs": True, "peek": 0.0, "trade": False},
              4000: {"hs": False, "peek": 0.0, "trade": False}}
    # headshot + peek reaction, no tier base
    assert segment_quality([1000], m, credit) == 250.0
    # traded kill: kill points minus the deter, still no tier base
    assert segment_quality([2000], m, credit) == 0.0
    # plain kill
    assert segment_quality([4000], m, credit) == 100.0


def test_segment_fuses_sibling_candidate_credits():
    # The clutch candidate carries no per-kill data; the insta/duel
    # siblings do. Fusion by tick must find it.
    clutch = _seg_moment("clutch_1v4", [1000, 2000])
    insta = {"tier": "insta_kill", "tier_rank": 7, "kill_ticks": [1000],
             "hs_ticks": {1000: 50.0}, "peek_ticks": {1000: 100.0},
             "trade_ticks": []}
    credit = kill_credit_index(None, None, [clutch, insta])
    assert segment_quality([1000], clutch, credit) == 250.0


def test_chain_best_segment_ranks_unbroken_triple_first():
    triple = _seg_moment("punch_up_single", [1000, 1100, 1200],
                         hs_ticks={1000: 50.0, 1100: 50.0, 1200: 50.0})
    scattered = _seg_moment("clutch_1v4", [1000, 5000, 9000, 13000],
                            end_tick=20000)
    credit = kill_credit_index(None, None, [triple, scattered])
    triple_best, _ = chain_segments(
        {**triple, "chained": True, "kill_ticks": [1000, 1100, 1200]}, 64,
        credit)
    scattered_best, segs = chain_segments(
        {**scattered, "kill_ticks": [1000, 5000, 9000, 13000]}, 64, credit)
    assert len(segs) == 4  # one window per scattered kill
    assert triple_best > scattered_best


def test_quality_missing_fields_never_crash():
    assert moment_quality({}) == moment_quality({})
    assert moment_quality({}) < moment_quality(_q("trade"))
    assert moment_quality({"tier_rank": 99, "kill_ticks": None,
                           "ttk": "nonsense"}) == moment_quality({})


def test_kyousuke_order_single_builds_to_chain():
    # r2 lone Deagle (11582) must open; the r19 2-kill chain closes.
    single = {"tier": "punch_up_single",
              "tier_rank": TIER_ORDER.index("punch_up_single"),
              "kill_ticks": [11582], "ttk": 0.125,
              "clutch_initial_count": "", "start_tick": 11262}
    chain = {"tier": "punch_up_single",
             "tier_rank": TIER_ORDER.index("punch_up_single"),
             "kill_ticks": [112654, 112737], "ttk": 0.4375,
             "clutch_initial_count": "", "start_tick": 112334}
    assert moment_quality(chain) > moment_quality(single)
    ordered = sorted([chain, single],
                     key=lambda m: (moment_quality(m), m["start_tick"]))
    assert [m["kill_ticks"] for m in ordered] == [[11582], [112654, 112737]]


# ── target fill (minimum segments to reach the bar) ────────────────

def _fchain(kill, quality, start=None, end=None):
    base = {"start_tick": (kill - 160) if start is None else start,
            "end_tick": (kill + 128) if end is None else end,
            "kill_ticks": [kill], "tier": "insta_kill",
            "tier_rank": TIER_ORDER.index("insta_kill"),
            "label": f"INSTA {kill}", "rank_reason": "r",
            "round": 11, "pov_steam_id": "x", "pov_nick": "x",
            "quality": quality}
    return base


def test_fill_takes_minimum_segments_to_bar():
    from cs2archive.pov.build_hook_timeline import fill_to_target
    # Each lone single plans exactly 2.00s uncapped: 8 segments = 16s.
    chains = [_fchain(1000 + i * 10000, quality=5000 - i) for i in range(9)]
    picked, total = fill_to_target(chains, tickrate=TICKRATE, min_seconds=15.0)
    assert len(picked) == 8
    assert total == 16.0
    assert [m["quality"] for m in picked] == [5000 - i for i in range(8)]


def test_fill_stops_at_first_reaching_bar():
    from cs2archive.pov.build_hook_timeline import fill_to_target
    chains = [_fchain(1000, quality=100), _fchain(20000, quality=9000)]
    picked, total = fill_to_target(chains, tickrate=TICKRATE, min_seconds=2.0)
    assert [m["kill_ticks"] for m in picked] == [[20000]]
    assert total == 2.0


def test_fill_short_of_bar_ships_nothing():
    from cs2archive.pov.build_hook_timeline import fill_to_target
    chains = [_fchain(1000, quality=100)]
    picked, total = fill_to_target(chains, tickrate=TICKRATE, min_seconds=15.0)
    assert picked == []
    assert total == 2.0


def test_fill_skips_unplannable_chains():
    from cs2archive.pov.build_hook_timeline import fill_to_target
    dead = _fchain(1000, quality=9999)
    dead["kill_ticks"] = []
    chains = [dead, _fchain(20000, quality=100), _fchain(30000, quality=50),
              _fchain(40000, quality=25)]
    picked, total = fill_to_target(chains, tickrate=TICKRATE, min_seconds=5.0)
    assert [m["kill_ticks"] for m in picked] == [[20000], [30000], [40000]]
    assert total == 6.0


def test_quota_blocks_new_chains_but_not_merges():
    from cs2archive.pov.build_hook_timeline import pick_moments
    far = _cand(70000)
    cands = [far, _cand(61341), _cand(61460)]
    picked = pick_moments(cands, TICKRATE, 1)
    # far takes the slot; the close pair still forms nothing (no free slot
    # for a NEW chain) — merging only applies to existing chains.
    assert len(picked) == 1
    assert picked[0]["kill_ticks"] == [70000]


def test_transitive_bridge_merges_two_chains():
    from cs2archive.pov.build_hook_timeline import pick_moments
    # p1=[100] and p2=[600] are separate (gap 500); bridge [350] joins p1,
    # and the grown union then absorbs p2 — one chain, fixpoint.
    p1 = _cand(100, start_tick=0, end_tick=400)
    p2 = _cand(600, start_tick=500, end_tick=900)
    bridge = _cand(350, start_tick=250, end_tick=650)
    picked = pick_moments([p1, p2, bridge], TICKRATE, 3)
    assert len(picked) == 1
    assert picked[0]["kill_ticks"] == [100, 350, 600]


def _hook_timeline(tmp_path, *, steam_id="7"):
    import json

    demo = tmp_path / "m.dem"
    demo.write_bytes(b"0")
    tl = {
        "picked": [{
            "label": "IN", "tier": "insta_kill", "round": 3,
            "start_tick": 1000, "end_tick": 1200, "kill_ticks": [1100],
            "pov_steam_id": steam_id, "pov_nick": "kyousuke",
        }],
        "demo_path": str(demo), "map": "de_mirage", "tickrate": 64,
        "player": {"steam_id": steam_id, "nick": "kyousuke"},
    }
    path = tmp_path / "hook_timeline.json"
    path.write_text(json.dumps(tl), encoding="utf-8")
    return path


def test_render_hook_uses_hardened_wrapper_and_version_gate(monkeypatch, tmp_path):
    """The hook must render through hook_aware (AfxHook query + ffmpeg
    delta + fast named-stage failure), never the legacy shorts poller —
    and it must pass the version gate first like the POV render."""
    import cs2archive.pov.render_hook as rh
    import cs2archive.pov.render_version_check as rvc

    gated: list = []
    monkeypatch.setattr(
        rvc, "assert_render_versions",
        lambda demo: gated.append(demo) or
        {"demo": "d", "cs2": "c", "hlae": "h", "csdm": "s"})
    monkeypatch.setattr(rh, "canonical_nick", lambda sid, nick: "kyousuke")
    monkeypatch.setattr(
        rh, "_player_cvars",
        lambda *a, **k: ([], {"source": "t", "screen_height": 960}))
    monkeypatch.setattr(rh, "_pov_kill_ticks", lambda *a, **k: [1100])
    monkeypatch.setattr(rh, "_swap_autoexec", lambda *a, **k: None)
    monkeypatch.setattr(rh, "_restore_autoexec", lambda *a, **k: None)
    calls: dict = {}

    def fake_hardened(cmd, label, outdir, *, hook_timeout=120.0,
                      hook_retries=2, **kw):
        calls.update(cmd=cmd, label=label, outdir=outdir,
                     timeout=hook_timeout, retries=hook_retries)
        return outdir / "seg.mp4"

    monkeypatch.setattr(rh, "run_csdm_hook_aware", fake_hardened)
    monkeypatch.setattr(rh, "_find_sequence_files",
                        lambda d, n: [tmp_path / "seg.mp4"])
    out = rh.render_hook(_hook_timeline(tmp_path), width=1280, height=960,
                         min_seconds=1.0)
    assert gated, "version gate must run before the CSDM launch"
    assert calls["label"] == "hook"
    assert calls["cmd"][:2] == [rh.CSDM, "video"]
    assert "--config-file" in calls["cmd"]
    # render_hook's hook_retries counts TOTAL attempts; the hardened
    # wrapper counts EXTRA attempts after the first.
    assert calls["retries"] == 1
    assert calls["timeout"] == 150.0
    assert out == tmp_path
    assert (tmp_path / "hook_render.json").is_file()


def test_render_hook_hook_failure_exits_loudly(monkeypatch, tmp_path):
    """A hardened-wrapper None (all attempts failed) must still fail the
    render loudly — the legacy path called sys.exit(1)."""
    import pytest

    import cs2archive.pov.render_hook as rh
    import cs2archive.pov.render_version_check as rvc

    monkeypatch.setattr(
        rvc, "assert_render_versions",
        lambda demo: {"demo": "d", "cs2": "c", "hlae": "h", "csdm": "s"})
    monkeypatch.setattr(rh, "canonical_nick", lambda sid, nick: "kyousuke")
    monkeypatch.setattr(
        rh, "_player_cvars",
        lambda *a, **k: ([], {"source": "t", "screen_height": 960}))
    monkeypatch.setattr(rh, "_pov_kill_ticks", lambda *a, **k: [1100])
    monkeypatch.setattr(rh, "_swap_autoexec", lambda *a, **k: None)
    monkeypatch.setattr(rh, "_restore_autoexec", lambda *a, **k: None)
    monkeypatch.setattr(rh, "run_csdm_hook_aware",
                        lambda *a, **k: None)
    with pytest.raises(SystemExit):
        rh.render_hook(_hook_timeline(tmp_path), width=1280, height=960,
                       min_seconds=1.0)
