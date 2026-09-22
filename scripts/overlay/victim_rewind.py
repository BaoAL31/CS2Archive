"""Detect POV kills that qualify for a victim-cam rewind.

Criteria: awp_flick / flick (yaw snap, victim dies within 0.2s; AWP is its own kind), insta kill (LOS TTK, not a trade),
clean thru-smoke, clean wallbang, long noscope. No knife. No CSDM insert here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from _pathsetup import ensure

ensure()

from shorts.flick import PRE_TICKS  # noqa: E402


@dataclass(frozen=True)
class DetectorConfig:
    """Tick windows and geometry. Values are at 64 tick unless noted."""

    tickrate: float = 64.0
    clean_fire_ticks: int = 64  # 1s isolated-shot window
    los_lookback_ticks: int = 128  # 2s
    los_stride: int = 4  # sample every 4 ticks (~62.5ms)
    ttk_ticks: int = 32  # 0.5s insta-kill cap
    trade_ticks: int = 128  # 2s: teammate died to this victim → not insta
    victim_min_hp: float = 90.0  # pre-kill HP; 20hp peeks are not impressive
    noscope_min_distance: float = 20.0  # player_death.distance is metres; skip close ones
    eye_standing: float = 64.0
    eye_duck_delta: float = 18.0  # standing 64 → full crouch ~46
    smoke_radius: float = 160.0


CFG = DetectorConfig()
TICKRATE = CFG.tickrate
CLEAN_FIRE_TICKS = CFG.clean_fire_ticks
LOS_LOOKBACK_TICKS = CFG.los_lookback_ticks
LOS_STRIDE = CFG.los_stride
TTK_TICKS = CFG.ttk_ticks
TRADE_TICKS = CFG.trade_ticks
VICTIM_MIN_HP = CFG.victim_min_hp
NOSCOPE_MIN_DISTANCE = CFG.noscope_min_distance
EYE_STANDING = CFG.eye_standing
EYE_DUCK_DELTA = CFG.eye_duck_delta
SMOKE_RADIUS = CFG.smoke_radius

BURST_WEAPONS = frozenset({"famas", "galilar", "galil ar", "galil"})
SNIPERS = frozenset({"awp", "ssg08", "ssg 08", "g3sg1", "scar20", "scar-20"})
NADE_WEAPONS = frozenset({
    "inferno", "hegrenade", "he grenade", "flashbang", "smokegrenade",
    "smoke grenade", "decoy", "decoy grenade", "incendiary", "molotov",
    "tagrenade", "ta grenade", "fraggrenade", "frag grenade",
    "world", "c4", "planted_c4",
})

# Valid labels. Order is check order only — overlapping kills keep every match.
REASONS = (
    "awp_flick",
    "flick",
    "insta_kill",
    "thru_smoke",
    "wallbang",
    "noscope",
)


def eye_z(origin_z: float, duck_amount: float = 0.0) -> float:
    duck = max(0.0, min(1.0, float(duck_amount or 0.0)))
    return float(origin_z) + EYE_STANDING - duck * EYE_DUCK_DELTA


def eye_pos(snap: dict) -> tuple[float, float, float]:
    return (
        float(snap["x"]),
        float(snap["y"]),
        eye_z(snap["z"], snap.get("duck_amount") or 0.0),
    )


def _weapon(k: dict) -> str:
    return str(k.get("weapon") or "").strip().lower()


def _is_zeus(weapon: str) -> bool:
    return weapon in ("zeus", "zeus x27")


def _is_knife(weapon: str) -> bool:
    if _is_zeus(weapon):
        return False
    return "knife" in weapon


def _is_awp(weapon: str) -> bool:
    return weapon == "awp"


def _is_gun(weapon: str) -> bool:
    if not weapon or weapon in NADE_WEAPONS:
        return False
    if _is_knife(weapon) or _is_zeus(weapon):
        return False
    return True


def _is_tk(k: dict) -> bool:
    """Same persistent team (2/3). Missing teams are not treated as TK."""
    try:
        at = int(k.get("attacker_team") or 0)
        vt = int(k.get("victim_team") or 0)
    except (TypeError, ValueError):
        return False
    return at >= 2 and vt >= 2 and at == vt


def is_trade(k: dict, kills: list[dict]) -> bool:
    """True when this victim just killed the attacker's teammate (classic trade)."""
    aid = str(k.get("attacker_sid") or "")
    vid = str(k.get("victim_sid") or "")
    tick = int(k["tick"])
    try:
        at = int(k.get("attacker_team") or 0)
    except (TypeError, ValueError):
        at = 0
    if not aid or not vid or at < 2:
        return False
    for other in kills:
        ot = int(other.get("tick") or 0)
        if ot >= tick or tick - ot > TRADE_TICKS:
            continue
        if str(other.get("attacker_sid") or "") != vid:
            continue
        if str(other.get("victim_sid") or "") == aid:
            continue
        try:
            vt = int(other.get("victim_team") or 0)
        except (TypeError, ValueError):
            vt = 0
        if vt == at:
            return True
    return False


def _as_bool(val) -> bool:
    if val is True or val is False:
        return val
    try:
        if val != val:
            return False
    except Exception:
        pass
    return bool(val)


def _headshot(k: dict) -> bool:
    if _as_bool(k.get("headshot")):
        return True
    return str(k.get("hitgroup") or "").strip().lower() == "head"


def fire_count(attacker_sid: str, t0: int, t1: int, fires: list[dict]) -> int:
    return sum(
        1
        for f in fires
        if str(f.get("player_sid") or "") == attacker_sid
        and t0 <= int(f["tick"]) <= t1
    )


def is_clean_shot(k: dict, fires: list[dict]) -> bool:
    """Isolated shot in the 1s before the kill. Burst rifles may fire twice."""
    if not _is_gun(_weapon(k)):
        return False
    aid = str(k.get("attacker_sid") or "")
    tick = int(k["tick"])
    n = fire_count(aid, tick - CLEAN_FIRE_TICKS, tick, fires)
    if n == 1:
        return True
    return n == 2 and _weapon(k) in BURST_WEAPONS


def segment_hits_sphere(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
    radius: float,
) -> bool:
    """True if segment AB comes within radius of C."""
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    ab2 = ab[0] * ab[0] + ab[1] * ab[1] + ab[2] * ab[2]
    if ab2 < 1e-12:
        d2 = ac[0] * ac[0] + ac[1] * ac[1] + ac[2] * ac[2]
        return d2 <= radius * radius
    t = (ac[0] * ab[0] + ac[1] * ab[1] + ac[2] * ab[2]) / ab2
    t = max(0.0, min(1.0, t))
    px = a[0] + t * ab[0] - c[0]
    py = a[1] + t * ab[1] - c[1]
    pz = a[2] + t * ab[2] - c[2]
    return px * px + py * py + pz * pz <= radius * radius


def smoke_occludes(
    eye_a: tuple[float, float, float],
    eye_b: tuple[float, float, float],
    tick: int,
    smokes: list[dict],
) -> bool:
    for s in smokes:
        if int(s["start"]) > tick or int(s["end"]) < tick:
            continue
        c = (float(s["x"]), float(s["y"]), float(s["z"]))
        if segment_hits_sphere(eye_a, eye_b, c, SMOKE_RADIUS):
            return True
    return False


def los_open_tick_from_flags(ticks: list[int], opens: list[bool]) -> int | None:
    """Start of the most recent peek, walking newest → oldest.

    From the kill, require two consecutive open samples (one-tick cracks are
    still blocked). Then walk back through that open run until a block. The
    first open after that block is ``los_open_time``. An earlier peek that
    closed before this run is ignored.

    Never-blocked lookback → None (plain one-tap). Open run shorter than two
    samples → None (noise).
    """
    if len(ticks) != len(opens) or len(opens) < 2:
        return None
    if not opens[-1] or not opens[-2]:
        return None
    i = len(opens) - 1
    while i >= 0 and opens[i]:
        i -= 1
    if i < 0:
        return None
    run_start = i + 1
    if len(opens) - run_start < 2:
        return None
    return ticks[run_start]


def _payload(k: dict, reason: str, los_open_tick: int | None) -> dict:
    hitgroup = k.get("hitgroup")
    if hitgroup is None or hitgroup == "":
        hitgroup = "head" if _headshot(k) else ""
    return {
        "round": int(k.get("round") or 0),
        "kill_tick": int(k["tick"]),
        "attacker_sid": str(k.get("attacker_sid") or ""),
        "victim_sid": str(k.get("victim_sid") or ""),
        "reason": reason,
        "weapon": _weapon(k),
        "hitgroup": str(hitgroup or ""),
        "los_open_tick": los_open_tick,
    }


def _mesh_open(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    mesh_open_fn,
) -> bool:
    if mesh_open_fn is None:
        return False
    return bool(mesh_open_fn(a, b))


def _snap_at(snaps: dict, tick: int, sid: str) -> dict | None:
    return snaps.get((int(tick), str(sid)))


def victim_hp_before_kill(vid: str, tick: int, snaps: dict) -> float | None:
    """Last positive HP at or before the kill tick (kill-tick health is often 0)."""
    for t in (tick - 1, tick - LOS_STRIDE, tick):
        s = snaps.get((int(t), str(vid)))
        if not s:
            continue
        try:
            hp = float(s.get("health") if s.get("health") is not None else -1)
        except (TypeError, ValueError):
            continue
        if hp > 0:
            return hp
    return None


def _los_samples(
    k: dict,
    snaps: dict,
    mesh_open_fn,
) -> tuple[list[int], list[bool]]:
    aid = str(k.get("attacker_sid") or "")
    vid = str(k.get("victim_sid") or "")
    kill = int(k["tick"])
    ticks = list(range(kill - LOS_LOOKBACK_TICKS, kill + 1, LOS_STRIDE))
    if ticks[-1] != kill:
        ticks.append(kill)
    opens: list[bool] = []
    kept: list[int] = []
    for t in ticks:
        sa = _snap_at(snaps, t, aid)
        sv = _snap_at(snaps, t, vid)
        if sa is None or sv is None:
            continue
        kept.append(t)
        opens.append(_mesh_open(eye_pos(sa), eye_pos(sv), mesh_open_fn))
    return kept, opens


def _reasons_for_kill(
    k: dict,
    *,
    kills: list[dict],
    fires: list[dict],
    snaps: dict,
    smokes: list[dict],
    flick_kills: set[tuple[str, int]],
    mesh_open_fn,
) -> tuple[list[str], dict]:
    weapon = _weapon(k)
    aid = str(k.get("attacker_sid") or "")
    vid = str(k.get("victim_sid") or "")
    tick = int(k["tick"])
    if not aid or not vid or aid == vid or _is_tk(k):
        return [], {}
    if weapon in NADE_WEAPONS or _is_zeus(weapon) or _is_knife(weapon):
        return [], {}

    ticks, opens = _los_samples(k, snaps, mesh_open_fn)
    los_est = los_open_tick_from_flags(ticks, opens)
    open_at_kill = bool(opens and opens[-1] and ticks[-1] == tick)
    peek = los_est is not None and tick - los_est <= TTK_TICKS
    snap = (aid, tick) in flick_kills and _is_gun(weapon)

    reasons: list[str] = []
    if snap and _is_awp(weapon):
        reasons.append("awp_flick")
    elif snap:
        reasons.append("flick")

    if peek and _is_gun(weapon) and not is_trade(k, kills):
        hp = victim_hp_before_kill(vid, tick, snaps)
        if hp is not None and hp >= VICTIM_MIN_HP:
            reasons.append("insta_kill")

    sa = _snap_at(snaps, tick, aid)
    sv = _snap_at(snaps, tick, vid)
    eyes = None
    if sa is not None and sv is not None:
        eyes = (eye_pos(sa), eye_pos(sv))

    if (
        _is_gun(weapon)
        and _as_bool(k.get("thrusmoke"))
        and is_clean_shot(k, fires)
        and open_at_kill
        and eyes is not None
        and smoke_occludes(eyes[0], eyes[1], tick, smokes)
    ):
        reasons.append("thru_smoke")

    try:
        penetrated = int(k.get("penetrated") or 0)
    except (TypeError, ValueError):
        penetrated = 0
    if _is_gun(weapon) and penetrated >= 1 and is_clean_shot(k, fires):
        reasons.append("wallbang")

    try:
        dist = float(k.get("distance") or 0.0)
    except (TypeError, ValueError):
        dist = 0.0
    if (
        _is_gun(weapon)
        and _as_bool(k.get("noscope"))
        and weapon in SNIPERS
        and dist > NOSCOPE_MIN_DISTANCE
    ):
        reasons.append("noscope")

    los_for_reason = {
        "awp_flick": los_est,
        "flick": los_est,
        "insta_kill": los_est,
        "thru_smoke": tick if open_at_kill else None,
    }
    return reasons, los_for_reason


def detect_rewinds(
    kills: list[dict],
    *,
    fires: list[dict] | None = None,
    snaps: dict | None = None,
    smokes: list[dict] | None = None,
    flick_kills: set[tuple[str, int]] | None = None,
    mesh_open_fn=None,
) -> list[dict]:
    """Return at most one rewind payload per kill. All matching reasons are kept."""
    fires = fires or []
    snaps = snaps or {}
    smokes = smokes or []
    flick_kills = flick_kills or set()
    out: list[dict] = []
    for k in kills:
        reasons, los_map = _reasons_for_kill(
            k,
            kills=kills,
            fires=fires,
            snaps=snaps,
            smokes=smokes,
            flick_kills=flick_kills,
            mesh_open_fn=mesh_open_fn,
        )
        if not reasons:
            continue
        # Label is the first match in check order; `reasons` is the full set.
        reason = reasons[0]
        los_tick = los_map.get(reason) if reason in ("awp_flick", "flick", "insta_kill", "thru_smoke") else None
        row = _payload(k, reason, los_tick)
        row["reasons"] = list(reasons)
        out.append(row)
    return out


def _closest_hit_open(map_name: str):
    from config import settings

    root = Path(settings.cs2util_root)
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    from scripts.render.map_collision import closest_hit

    def mesh_open(a, b) -> bool:
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < 1e-6:
            return True
        return closest_hit(a, (dx, dy, dz), dist, map_name=map_name) is None

    return mesh_open


def detect_from_demo(demo_path: Path, player: str | None = None) -> list[dict]:
    """Parse a demo (sync). Heavy demoparser I/O — not for an async loop.

    Pipeline callers should use ``detect_from_demo_async`` (``asyncio.to_thread``).
    """
    import demoparser2 as dp
    from shorts.build_short_timeline import (
        _as_event_df,
        _collect_flick_kills,
        _collect_smokes,
        _collect_weapon_fires,
        _round_for_tick,
        _sid,
    )

    parser = dp.DemoParser(str(demo_path))
    deaths = _as_event_df(parser.parse_event("player_death"))
    freeze_end = _as_event_df(parser.parse_event("round_freeze_end"))
    round_start = _as_event_df(parser.parse_event("round_start"))
    info = parser.parse_player_info()
    team_by_sid: dict[str, int] = {}
    if info is not None and not getattr(info, "empty", True):
        for _, row in info.iterrows():
            sid = _sid(row.get("steamid"))
            if sid:
                try:
                    team_by_sid[sid] = int(row.get("team_number") or 0)
                except (TypeError, ValueError):
                    team_by_sid[sid] = 0
    first_freeze = None
    if freeze_end is not None and not freeze_end.empty:
        first_freeze = int(freeze_end["tick"].min())
    try:
        header = parser.parse_header()
        map_name = str(header.get("map_name", "") or "")
    except Exception:
        map_name = ""

    round_starts: list[tuple[int, int]] = []
    if round_start is not None and not round_start.empty:
        for t, rn in zip(round_start["tick"].tolist(), round_start["round"].tolist()):
            round_starts.append((int(t), int(rn or 0)))
    round_starts.sort()

    fires_raw = _collect_weapon_fires(parser, first_freeze)
    flick_kills = _collect_flick_kills(
        parser, deaths, first_freeze, converted=True,
    )
    smokes = _collect_smokes(parser)

    kills: list[dict] = []
    if deaths is not None and not deaths.empty:
        for _, row in deaths.sort_values("tick").iterrows():
            tick = int(row["tick"])
            if first_freeze is not None and tick < first_freeze:
                continue
            aid = _sid(row.get("attacker_steamid"))
            vid = _sid(row.get("user_steamid"))
            if player and aid != str(player):
                continue
            weapon = str(row.get("weapon", "") or "").strip().lower()
            kills.append({
                "tick": tick,
                "round": _round_for_tick(tick, round_starts, first_freeze),
                "attacker_sid": aid,
                "victim_sid": vid,
                "attacker_team": team_by_sid.get(aid, 0),
                "victim_team": team_by_sid.get(vid, 0),
                "weapon": weapon,
                "hitgroup": str(row.get("hitgroup", "") or ""),
                "headshot": _as_bool(row.get("headshot")),
                "penetrated": int(row.get("penetrated") or 0) if row.get("penetrated") == row.get("penetrated") else 0,
                "noscope": _as_bool(row.get("noscope")),
                "thrusmoke": _as_bool(row.get("thrusmoke")),
                "distance": float(row.get("distance") or 0.0) if row.get("distance") == row.get("distance") else 0.0,
            })

    needed: set[int] = set()
    sids: set[str] = set()
    for k in kills:
        tick = int(k["tick"])
        sids.add(k["attacker_sid"])
        sids.add(k["victim_sid"])
        for t in range(tick - LOS_LOOKBACK_TICKS, tick + 1, LOS_STRIDE):
            needed.add(t)
        needed.add(tick)
        needed.add(tick - 1)
        for t in range(tick - PRE_TICKS, tick + 1):
            needed.add(t)

    snaps: dict[tuple[int, str], dict] = {}
    if needed and sids:
        fields = ["X", "Y", "Z", "pitch", "yaw", "duck_amount", "is_scoped", "flash_duration", "health"]
        try:
            tdf = parser.parse_ticks(fields, ticks=sorted(needed))
        except Exception:
            tdf = None
        if tdf is not None and not getattr(tdf, "empty", True):
            for _, row in tdf.iterrows():
                sid = _sid(row.get("steamid"))
                if sid not in sids:
                    continue
                try:
                    snaps[(int(row["tick"]), sid)] = {
                        "x": float(row.get("X") if row.get("X") == row.get("X") else row.get("x") or 0),
                        "y": float(row.get("Y") if row.get("Y") == row.get("Y") else row.get("y") or 0),
                        "z": float(row.get("Z") if row.get("Z") == row.get("Z") else row.get("z") or 0),
                        "pitch": float(row.get("pitch") or 0),
                        "yaw": float(row.get("yaw") or 0),
                        "duck_amount": float(row.get("duck_amount") or 0),
                        "is_scoped": _as_bool(row.get("is_scoped")),
                        "flash_duration": float(row.get("flash_duration") or 0),
                        "health": float(row.get("health") or 0),
                    }
                except (TypeError, ValueError):
                    continue

    mesh_open_fn = _closest_hit_open(map_name) if map_name else None
    return detect_rewinds(
        kills,
        fires=fires_raw,
        snaps=snaps,
        smokes=smokes,
        flick_kills=flick_kills,
        mesh_open_fn=mesh_open_fn,
    )


async def detect_from_demo_async(demo_path: Path, player: str | None = None) -> list[dict]:
    """Thread off ``detect_from_demo`` so the async pipeline is not blocked."""
    return await asyncio.to_thread(detect_from_demo, demo_path, player)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Detect victim-rewind kills in a demo")
    p.add_argument("demo")
    p.add_argument("--player", default=None, help="POV steam id (attacker)")
    args = p.parse_args(argv)
    rows = detect_from_demo(Path(args.demo), player=args.player)
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
