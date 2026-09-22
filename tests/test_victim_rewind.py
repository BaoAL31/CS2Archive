"""Victim rewind detector: locked criteria, no demo required."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from overlay.victim_rewind import (
    CLEAN_FIRE_TICKS,
    LOS_LOOKBACK_TICKS,
    LOS_STRIDE,
    TTK_TICKS,
    detect_rewinds,
    eye_z,
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
        "weapon", "hitgroup", "los_open_tick", "reasons",
    }
    assert got[0]["reasons"] == ["insta_kill"]


def test_insta_kill_m4_body_still_qualifies():
    """Kill within LOS TTK counts; HS is not required (M4 HS often is not a kill)."""
    got = detect_rewinds(
        [_kill(weapon="m4a1", headshot=False, hitgroup="chest")],
        snaps=_snaps_lookback(),
        mesh_open_fn=_open_at_fn(_peek_open_at()),
    )
    assert got[0]["reason"] == "insta_kill"


def test_insta_kill_rejects_low_hp():
    got = detect_rewinds(
        [_kill(weapon="m4a1", headshot=False, hitgroup="chest")],
        snaps=_snaps_lookback(health=20.0),
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
    """Early peek would fail TTK; hide; second peek at the kill qualifies."""
    ticks = [0, 4, 8, 12, 16, 20]
    opens = [False, True, True, False, True, True]
    assert los_open_tick_from_flags(ticks, opens) == 16


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
