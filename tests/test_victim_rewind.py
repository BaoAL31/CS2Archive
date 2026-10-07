"""Victim rewind detector: locked criteria, no demo required."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cs2archive.overlay.victim_rewind import (
    CLEAN_FIRE_TICKS,
    LOS_LOOKBACK_TICKS,
    LOS_STRIDE,
    TTK_TICKS,
    detect_rewinds,
    eye_z,
    has_prior_exposure,
    is_clean_shot,
    los_open_tick_from_flags,
    smoke_occludes,
)


AID = "1"
VID = "2"
KILL = 2000


def _snap(tick: int, sid: str, **kw) -> tuple[tuple[int, str], dict]:
    row = {
        "x": float(kw.get("x", tick if sid == AID else tick + 80)),
        "y": float(kw.get("y", 0.0)),
        "z": float(kw.get("z", 0.0)),
        "pitch": float(kw.get("pitch", 0.0)),
        "yaw": float(kw.get("yaw", 0.0)),
        "duck_amount": float(kw.get("duck_amount", 0.0)),
        "is_scoped": bool(kw.get("is_scoped", False)),
        "flash_duration": float(kw.get("flash_duration", 0.0)),
        "health": float(kw.get("health", 100.0)),
    }
    return (tick, sid), row


def _snaps_lookback(kill: int = KILL, **extra_v) -> dict:
    out = {}
    ticks = list(range(kill - LOS_LOOKBACK_TICKS, kill + 1, LOS_STRIDE))
    if ticks[-1] != kill:
        ticks.append(kill)
    for t in ticks:
        k, a = _snap(t, AID, x=float(t), y=0.0)
        out[k] = a
        k2, v = _snap(t, VID, x=float(t) + 80.0, y=0.0, **extra_v)
        out[k2] = v
    return out


def _open_at_fn(open_at: dict[int, bool]):
    """Map attacker-eye X (encoded as tick) to a canned open/blocked flag."""
    def open_fn(a, b):
        t = int(round(a[0]))
        return bool(open_at.get(t, False))

    return open_fn


def _kill(**kw) -> dict:
    row = {
        "tick": KILL,
        "round": 3,
        "attacker_sid": AID,
        "victim_sid": VID,
        "weapon": "ak47",
        "hitgroup": "head",
        "headshot": True,
        "penetrated": 0,
        "noscope": False,
        "thrusmoke": False,
        "distance": 100.0,
        "attacker_team": 2,
        "victim_team": 3,
    }
    row.update(kw)
    return row


def _within_ttk(kill_tick: int, los_open_tick: int) -> bool:
    return kill_tick - los_open_tick <= TTK_TICKS


def test_eye_z_ducks():
    assert eye_z(0.0, 0.0) == 64.0
    assert eye_z(0.0, 1.0) == 46.0
    assert eye_z(10.0, 0.5) == 10.0 + 64.0 - 9.0


def test_clean_shot_one_fire():
    k = _kill()
    fires = [{"tick": KILL - 4, "player_sid": AID}]
    assert is_clean_shot(k, fires)


def test_clean_shot_rejects_spray():
    k = _kill()
    fires = [{"tick": KILL - i, "player_sid": AID} for i in range(0, 20, 2)]
    assert not is_clean_shot(k, fires)


def test_clean_shot_allows_galil_burst():
    k = _kill(weapon="galilar")
    fires = [
        {"tick": KILL - 2, "player_sid": AID},
        {"tick": KILL, "player_sid": AID},
    ]
    assert is_clean_shot(k, fires)


def test_los_never_blocked_is_none():
    ticks = list(range(0, 20, 4))
    opens = [True] * len(ticks)
    assert los_open_tick_from_flags(ticks, opens) is None


def test_los_one_open_tick_is_noise():
    ticks = [0, 4, 8, 12]
    opens = [False, False, True, False]
    assert los_open_tick_from_flags(ticks, opens) is None


def test_los_two_consecutive_opens_after_block():
    ticks = [0, 4, 8, 12]
    opens = [False, False, True, True]
    assert los_open_tick_from_flags(ticks, opens) == 8


def _peek_open_at():
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    if ticks[-1] != KILL:
        ticks.append(KILL)
    open_at = {t: t >= KILL - TTK_TICKS for t in ticks}
    open_at[KILL - TTK_TICKS - LOS_STRIDE] = False
    return open_at


def test_insta_kill_qualifies_on_ttk():
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert len(got) == 1
    assert got[0]["reason"] == "insta_kill"
    assert got[0]["los_open_tick"] is not None
    assert _within_ttk(KILL, got[0]["los_open_tick"])
    assert set(got[0]) == {
        "round", "kill_tick", "attacker_sid", "victim_sid", "reason",
        "weapon", "hitgroup", "los_open_tick", "reasons", "victim_hp",
        "flick_speed", "victim_hold_deg",
    }
    assert got[0]["reasons"] == ["insta_kill"]


def test_slow_peek_is_not_insta_but_stays_visible():
    """0.5s first contact: over the 0.35s insta cap, inside the 0.5s
    measured-contact window — surfaces as ``peek`` (hook punch-up proof),
    never as an insta moment."""
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    if ticks[-1] != KILL:
        ticks.append(KILL)
    open_at = {t: t >= KILL - 32 for t in ticks}
    open_at[KILL - 36] = False
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert len(got) == 1
    assert got[0]["reasons"] == ["peek"]
    assert got[0]["los_open_tick"] == KILL - 32


def test_boundary_20_ticks_is_insta():
    """134957 reality: 20-tick TTK (0.3125s) fits the 0.35s cap."""
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    if ticks[-1] != KILL:
        ticks.append(KILL)
    open_at = {t: t >= KILL - 20 for t in ticks}
    open_at[KILL - 24] = False
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert [r["reasons"] for r in got] == [["insta_kill"]]
    assert got[0]["los_open_tick"] == KILL - 20


def test_boundary_24_ticks_is_peek():
    """24-tick TTK (0.375s) clears the cap but stays measured contact."""
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    if ticks[-1] != KILL:
        ticks.append(KILL)
    open_at = {t: t >= KILL - 24 for t in ticks}
    open_at[KILL - 28] = False
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert [r["reasons"] for r in got] == [["peek"]]


def test_insta_kill_m4_body_still_qualifies():
    """Kill within LOS TTK counts; HS is not required (M4 HS often is not a kill)."""
    got = detect_rewinds(
        [_kill(weapon="m4a1", headshot=False, hitgroup="chest")],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert got[0]["reason"] == "insta_kill"


def test_insta_kill_accepts_tagged_victim():
    # No HP requirement: a fast LOS flick onto a damaged victim still
    # qualifies (kyousuke 61341/61460 at 16/20 HP).
    got = detect_rewinds(
        [_kill(weapon="m4a1", headshot=False, hitgroup="chest")],
        snaps=_snaps_lookback(health=20.0),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert [r["reasons"] for r in got] == [["insta_kill"]]


def test_insta_kill_still_rejects_missing_snaps():
    # Missing snapshots fail closed (hp None -> reject).
    got = detect_rewinds(
        [_kill(weapon="m4a1", headshot=False, hitgroup="chest")],
        snaps={},
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert got == []


def test_insta_kill_rejects_already_visible():
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    open_at = {t: True for t in ticks}
    open_at[KILL] = True
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert got == []


def test_flick_and_insta_can_overlap():
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert set(got[0]["reasons"]) == {"flick", "insta_kill"}
    assert got[0]["los_open_tick"] is not None


def test_thru_smoke_needs_puff_on_ray():
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    open_at = {t: True for t in ticks}
    k = _kill(headshot=False, hitgroup="chest", thrusmoke=True)
    fires = [{"tick": KILL, "player_sid": AID}]
    snaps = _snaps_lookback()
    base = {
        "kills": [k],
        "fires": fires,
        "snaps": snaps,
        "mesh_open_fn": _open_at_fn(open_at),
    }
    assert detect_rewinds(**base, smokes=[]) == []
    smokes = [{
        "x": KILL + 40.0,
        "y": 0.0,
        "z": 64.0,
        "start": KILL - 10,
        "end": KILL + 10,
    }]
    got = detect_rewinds(**base, smokes=smokes)
    assert got[0]["reason"] == "thru_smoke"


def test_wallbang_any_gun_clean():
    k = _kill(weapon="deagle", headshot=False, hitgroup="chest", penetrated=1)
    fires = [{"tick": KILL, "player_sid": AID}]
    got = detect_rewinds([k], fires=fires)
    assert got[0]["reason"] == "wallbang"
    assert got[0]["los_open_tick"] is None


def test_wallbang_spray_rejected():
    k = _kill(weapon="ak47", headshot=False, hitgroup="chest", penetrated=1)
    fires = [{"tick": KILL - i, "player_sid": AID} for i in range(0, CLEAN_FIRE_TICKS, 4)]
    assert detect_rewinds([k], fires=fires) == []


def test_noscope_close_body_rejected():
    k = _kill(weapon="awp", headshot=False, hitgroup="chest", noscope=True, distance=9)
    assert detect_rewinds([k]) == []


def test_noscope_close_hs_rejected():
    k = _kill(weapon="awp", headshot=True, hitgroup="head", noscope=True, distance=9)
    assert detect_rewinds([k]) == []


def test_noscope_long_body_ok():
    k = _kill(weapon="awp", headshot=False, hitgroup="chest", noscope=True, distance=25)
    got = detect_rewinds([k])
    assert got[0]["reason"] == "noscope"


def test_knife_never_qualifies():
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    snaps = {}
    for t in ticks + [KILL]:
        snaps[(t, AID)] = {
            "x": 0.0, "y": 100.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0,
            "duck_amount": 0.0, "is_scoped": False, "flash_duration": 0.0, "health": 100.0,
        }
        snaps[(t, VID)] = {
            "x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0,
            "duck_amount": 0.0, "is_scoped": False, "flash_duration": 3.0, "health": 100.0,
        }
    k = _kill(weapon="knife", headshot=False, hitgroup="")
    assert detect_rewinds([k], snaps=snaps, mesh_open_fn=lambda a, b: True) == []


def test_insta_kill_rejects_trade():
    prior = _kill(
        tick=KILL - 64,
        attacker_sid=VID,
        victim_sid="9",
        attacker_team=3,
        victim_team=2,
        headshot=False,
        hitgroup="chest",
    )
    k = _kill()
    got = detect_rewinds(
        [prior, k],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert got == []


def test_insta_kill_rejects_trade_with_pov_scoped_kills():
    """Production path: detect_from_demo filters to POV kills, so the trade
    exclusion must run against the FULL kill list (trade_kills). Teammate
    dies at the victim's hands 6 ticks before the POV's kill — same shape
    as kyousuke/Mirage r9 (68429 -> 68435)."""
    prior = _kill(
        tick=KILL - 6,
        attacker_sid=VID,
        victim_sid="9",
        attacker_team=3,
        victim_team=2,
        headshot=False,
        hitgroup="chest",
    )
    k = _kill()
    got = detect_rewinds(
        [k],
        trade_kills=[prior, k],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert got == []


def test_flick_without_peek_still_qualifies():
    """Yaw-flick is enough; headshot, peek, and isolated fire are not required."""
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    open_at = {t: True for t in ticks}
    k = _kill(headshot=False, hitgroup="chest")
    got = detect_rewinds(
        [k],
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert got[0]["reasons"] == ["flick"]
    assert got[0]["los_open_tick"] is None


def test_awp_flick_is_its_own_kind():
    k = _kill(weapon="awp", headshot=False, hitgroup="chest")
    got = detect_rewinds(
        [k],
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
    )
    assert got[0]["reasons"] == ["awp_flick"]
    assert got[0]["reason"] == "awp_flick"


def test_scout_flick_is_generic_not_awp():
    k = _kill(weapon="ssg08", headshot=False, hitgroup="chest")
    got = detect_rewinds(
        [k],
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
    )
    assert got[0]["reasons"] == ["flick"]


def test_flick_spray_still_qualifies():
    """M4/rifle burst after the snap still counts; convert is the 0.2s kill window."""
    k = _kill(headshot=False, hitgroup="chest")
    fires = [
        {"tick": KILL - 10, "player_sid": AID},
        {"tick": KILL - 5, "player_sid": AID},
        {"tick": KILL, "player_sid": AID},
    ]
    got = detect_rewinds(
        [k],
        fires=fires,
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
    )
    assert got[0]["reasons"] == ["flick"]


def test_los_most_recent_peek_not_first():
    """los_open_tick_from_flags still reports the most recent run start.

    Whether that run counts as first contact is the insta gate's job
    (has_prior_exposure) — see the repeek tests below.
    """
    ticks = [0, 4, 8, 12, 16, 20]
    opens = [False, True, True, False, True, True]
    assert los_open_tick_from_flags(ticks, opens) == 16
    assert has_prior_exposure(ticks, opens) is True


def test_prior_exposure_clean_first_contact_is_false():
    ticks = [0, 4, 8, 12]
    opens = [False, False, True, True]
    assert has_prior_exposure(ticks, opens) is False


def test_prior_exposure_single_crack_is_noise():
    # One stray open sample early in the window must not kill a legit
    # first-contact insta (mesh edge flicker, not a real sighting).
    ticks = [0, 4, 8, 12, 16, 20]
    opens = [False, True, False, False, True, True]
    assert has_prior_exposure(ticks, opens) is False


def test_prior_exposure_needs_no_final_run():
    ticks = [0, 4, 8, 12]
    opens = [True, True, True, True]
    assert has_prior_exposure(ticks, opens) is False


def _repeek_open_at():
    """124655 replay: long open, short hide, repeek kill (prefire, not insta)."""
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    if ticks[-1] != KILL:
        ticks.append(KILL)
    open_at = {t: False for t in ticks}
    for t in ticks:
        if KILL - 72 <= t <= KILL - 20:
            open_at[t] = True  # first sighting (~0.8s)
        if t >= KILL - 4:
            open_at[t] = True  # repeek + prefire (TTK 0.06s)
    return open_at


def test_insta_kill_rejects_repeek_prefire():
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_repeek_open_at()),
    )
    assert got == []


def test_tk_rejected():
    k = _kill(weapon="deagle", headshot=False, hitgroup="chest", penetrated=1,
              attacker_team=2, victim_team=2)
    fires = [{"tick": KILL, "player_sid": AID}]
    assert detect_rewinds([k], fires=fires) == []


def test_thru_smoke_beats_wallbang():
    ticks = list(range(KILL - LOS_LOOKBACK_TICKS, KILL + 1, LOS_STRIDE))
    open_at = {t: True for t in ticks}
    k = _kill(
        headshot=False, hitgroup="chest", thrusmoke=True, penetrated=1,
    )
    fires = [{"tick": KILL, "player_sid": AID}]
    smokes = [{
        "x": KILL + 40.0, "y": 0.0, "z": 64.0,
        "start": KILL - 10, "end": KILL + 10,
    }]
    got = detect_rewinds(
        [k], fires=fires, snaps=_snaps_lookback(), smokes=smokes,
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert got[0]["reason"] == "thru_smoke"


def test_flick_and_noscope_can_overlap():
    k = _kill(weapon="awp", headshot=True, hitgroup="head", noscope=True, distance=25)
    got = detect_rewinds(
        [k],
        snaps=_snaps_lookback(),
        flick_kills={(AID, KILL)},
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert "awp_flick" in got[0]["reasons"]
    assert "flick" not in got[0]["reasons"]
    assert "noscope" in got[0]["reasons"]


def test_nade_rejected():
    assert detect_rewinds([_kill(weapon="hegrenade")]) == []


def test_smoke_occludes_midpoint():
    a = (0.0, 0.0, 64.0)
    b = (200.0, 0.0, 64.0)
    smokes = [{"x": 100.0, "y": 0.0, "z": 64.0, "start": 1, "end": 10}]
    assert smoke_occludes(a, b, 5, smokes)
    assert not smoke_occludes(a, b, 5, [{"x": 100.0, "y": 500.0, "z": 64.0, "start": 1, "end": 10}])


# ── flick speed + victim hold angle (hook quality inputs) ──────────

from cs2archive.overlay.victim_rewind import (  # noqa: E402
    angle_between_deg,
    flick_speed,
    victim_hold_deg,
    view_dir,
)


def test_view_dir_wrap_free():
    # The ±180 yaw seam must read as ~2 deg, not ~358.
    a = {"yaw": 179.0, "pitch": 0.0}
    b = {"yaw": -179.0, "pitch": 0.0}
    assert angle_between_deg(view_dir(a), view_dir(b)) == pytest.approx(2.0, abs=0.1)


def test_flick_speed_measures_sweep():
    k = {"tick": 2000, "attacker_sid": AID}
    snaps = {}
    for t in range(1980, 2001):
        snaps[(t, AID)] = {"x": 0.0, "y": 0.0, "z": 0.0,
                           "yaw": 0.0, "pitch": 0.0,
                           "duck_amount": 0.0, "health": 100.0}
    # 5 deg/tick sweep over the last 4 ticks -> 320 deg/s.
    for t in range(1997, 2001):
        snaps[(t, AID)]["yaw"] = (t - 1997) * 5.0
    assert flick_speed(k, snaps) == pytest.approx(320.0, rel=0.02)


def test_flick_speed_stationary_is_none():
    k = {"tick": 2000, "attacker_sid": AID}
    snaps = {(t, AID): {"yaw": 10.0, "pitch": 0.0} for t in range(1980, 2001)}
    assert flick_speed(k, snaps) is None


def test_victim_hold_dead_on_and_back_shot():
    k = {"tick": 2000, "attacker_sid": AID, "victim_sid": VID}
    # Attacker 1000uu along +X from the victim.
    sv = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "pitch": 0.0,
          "duck_amount": 0.0}
    sa = {"x": 1000.0, "y": 0.0, "z": 0.0, "yaw": 180.0, "pitch": 0.0,
          "duck_amount": 0.0}
    snaps = {(2000, VID): sv, (2000, AID): sa}
    assert victim_hold_deg(k, 2000, snaps) == pytest.approx(0.0, abs=0.1)
    sv_back = dict(sv, yaw=90.0)  # victim looking +Y, attacker along +X
    snaps[(2000, VID)] = sv_back
    assert victim_hold_deg(k, 2000, snaps) == pytest.approx(90.0, abs=0.1)


def test_victim_hold_fails_closed():
    k = {"tick": 2000, "attacker_sid": AID, "victim_sid": VID}
    assert victim_hold_deg(k, None, {}) is None          # no LOS
    assert victim_hold_deg(k, 2000, {}) is None           # no snapshots
    # Stacked players: direction is meaningless.
    s = {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "pitch": 0.0,
         "duck_amount": 0.0}
    snaps = {(2000, VID): s, (2000, AID): dict(s)}
    assert victim_hold_deg(k, 2000, snaps) is None


def test_rows_carry_flick_and_hold_fields():
    open_at = _peek_open_at()
    got = detect_rewinds(
        [_kill()],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(open_at),
    )
    assert got, "insta row expected"
    assert "flick_speed" in got[0] and "victim_hold_deg" in got[0]
    # Insta-only (no flick detector hit): raw peak deg/s must not surface —
    # hook quality would treat it as a flick bonus.
    assert "flick" not in got[0]["reasons"] and "awp_flick" not in got[0]["reasons"]
    assert got[0]["flick_speed"] is None


def test_flick_speed_only_on_detected_flick():
    # Same kill + dense snapshots that measure a real sweep: only when the
    # flick detector tagged the kill does flick_speed ride along.
    open_at = _peek_open_at()
    snaps = _snaps_lookback()
    for t in range(KILL - 24, KILL + 1):
        snaps.setdefault((t, AID), {"x": 0.0, "y": 0.0, "z": 64.0,
                                    "yaw": 0.0, "pitch": 0.0,
                                    "duck_amount": 0.0, "health": 100.0})
        snaps[(t, AID)]["yaw"] = (t - (KILL - 24)) * 5.0  # 320 deg/s peak
    plain = detect_rewinds(
        [_kill()], snaps=snaps, mesh_open_fn=_open_at_fn(open_at))
    assert plain and plain[0]["flick_speed"] is None
    tagged = detect_rewinds(
        [_kill()], snaps=snaps, mesh_open_fn=_open_at_fn(open_at),
        flick_kills={(AID, KILL)})
    assert "flick" in tagged[0]["reasons"] or "awp_flick" in tagged[0]["reasons"]
    assert tagged[0]["flick_speed"] == pytest.approx(320.0, rel=0.02)
