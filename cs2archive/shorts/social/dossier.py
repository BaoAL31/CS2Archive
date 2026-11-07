"""LLM-readable moment dossiers built from existing local evidence.

One dossier per reliably linked moment (an exact AllStar clip ↔ demo alignment,
or a verified/pending render-ledger record). Each dossier is a compact, versioned
JSON document plus a text rendering, assembled **only** from data already on
disk: the cached action timeline (`data/shorts-foundation/_timelines/*.json`),
the staged clip folder (`clips/<id>/{alignment,match,raw_allstar}.json`,
`events.parquet`, optionally `state.parquet`) and the social-evidence database.

Hard rules:

* Nothing is invented. Every field either comes from a named source or is
  reported under ``unknowns`` / ``availability``.
* Round state, economy and man-advantage come from the timeline's own `rounds`
  / `stakes` evidence; they are never reconstructed from kills alone.
* The AllStar map+player+round+kill-count alignment is **plausible, not proof**
  of the original clip's video contents. Dossiers say so explicitly
  (``clip_verification``) and keep ``published_clip_bounds`` unknown, because the
  original clip's trim is not recoverable from a demo.
* The kill-anchored window is labelled as an *inferred* window, distinct from
  both the round bounds and the (unknown) published clip bounds.
* No LOS, no intent, no per-opponent equipment beyond the weapon recorded on that
  opponent's own death event.

Writes are atomic (temp file + ``os.replace``) and idempotent (an existing
dossier with the same version and source hash is left alone unless ``--force``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from cs2archive.shorts.social.safety import dumps, iso, sha256_text

DOSSIER_VERSION = 1
DEFAULT_TICKRATE = 64
# How far around a kill window neighbouring events are quoted (ticks).
CONTEXT_BEFORE_TICKS = 128
CONTEXT_AFTER_TICKS = 64

BOUNDS_SOURCE_INFERRED = "kill_anchored_inferred"
CLIP_VERIFICATION_NOTE = (
    "AllStar map+player+round+kill-count alignment is plausible evidence, not "
    "proof of the original published clip's contents; the clip's own trim is not "
    "recoverable from a demo.")

UNKNOWN_BASELINE = (
    "line_of_sight",
    "player_intent",
    "per_opponent_loadout (only the weapon on that opponent's own death is known)",
    "ammunition",
    "armor_of_attacker (victim armor is known from the hurt event)",
    "movement_speed",
    "audio_and_comms",
    "published_clip_trim",
)


def tick_to_seconds(tick: int | float | None, tickrate: int = DEFAULT_TICKRATE,
                    origin: int | None = None) -> float | None:
    if tick is None:
        return None
    base = tick if origin is None else (tick - origin)
    return round(base / float(tickrate or DEFAULT_TICKRATE), 3)


def team_label(team_number, sides_by_round: dict | None, round_no: int | None):
    """T/CT for a team number in a round, straight from the recorded sides."""
    if team_number is None or not sides_by_round or round_no is None:
        return None
    sides = sides_by_round.get(str(round_no)) or sides_by_round.get(round_no)
    if not sides:
        return None
    for label, number in sides.items():
        if number == team_number:
            return label
    return None


def org_names(demo_path: str | None) -> tuple[str | None, str | None]:
    """Team slugs from the match folder (`2398717-falcons-vs-tyloo-...`)."""
    text = str(demo_path or "").replace("\\", "/")
    folder = text.split("/")[-2] if "/" in text else text
    if "-vs-" not in folder.lower():
        return None, None
    left, _, right = folder.partition("-vs-")
    first = [p for p in left.split("-") if p]
    if first and first[0].isdigit():
        first = first[1:]
    home = first[-1] if first else None
    rest = [p for p in right.split("-") if p]
    away = rest[0] if rest else None
    return home, away


@dataclass
class DossierStats:
    """Bookkeeping for a build pass: completed vs skipped vs failed."""

    built: int = 0
    skipped_current: int = 0
    failed: int = 0
    moments_seen: int = 0
    by_source: dict = field(default_factory=dict)
    failures: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"built": self.built, "skipped_current": self.skipped_current,
                "failed": self.failed, "moments_seen": self.moments_seen,
                "by_source": dict(sorted(self.by_source.items())),
                "failures": self.failures[:20]}


def _player_info(timeline: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    info = (timeline.get("raw_player_info") or {})
    for row in info.get("rows") or []:
        sid = str(row.get("steamid") or "")
        if sid:
            out[sid] = {"nick": row.get("name"),
                        "team_number": row.get("team_number")}
    for entry in timeline.get("pros") or []:
        sid = str(entry.get("steam_id") or "")
        if sid and sid in out:
            out[sid]["nick"] = out[sid]["nick"] or entry.get("nick")
            out[sid]["is_pro"] = True
        elif sid:
            out[sid] = {"nick": entry.get("nick"), "is_pro": True}
    return out


def _raw_rows(timeline: dict, key: str) -> list[dict]:
    payload = (timeline.get("raw_events") or {}).get(key) or {}
    return list(payload.get("rows") or [])


def _window(alignment: dict | None, kill_ticks: list[int]) -> tuple[int | None, int | None]:
    if alignment:
        start = alignment.get("tick_start")
        end = alignment.get("tick_end")
        if start is not None and end is not None:
            return int(start), int(end)
    if kill_ticks:
        return kill_ticks[0] - CONTEXT_BEFORE_TICKS, kill_ticks[-1] + CONTEXT_AFTER_TICKS
    return None, None


def _in_window(tick, lo, hi) -> bool:
    if tick is None or lo is None or hi is None:
        return False
    return lo <= int(tick) <= hi


def _alive_counts(round_info: dict | None, tick: int | None) -> dict:
    """Man advantage at a tick, from the round's recorded alive samples."""
    if not round_info or tick is None:
        return {}
    samples = [s for s in round_info.get("alive") or []
               if s.get("tick") is not None and int(s["tick"]) <= int(tick)]
    if not samples:
        return {}
    latest = max(samples, key=lambda s: int(s["tick"]))
    return {str(k): v for k, v in (latest.get("teams") or {}).items()}


def _clutch_context(round_info: dict | None, player_team, first_kill_tick):
    """Only what the alive samples literally show — no inferred intent."""
    counts = _alive_counts(round_info, first_kill_tick)
    if not counts or player_team is None:
        return None
    mine = counts.get(str(player_team))
    theirs = [v for k, v in counts.items() if str(k) != str(player_team)]
    if mine is None or not theirs:
        return None
    enemy = max(theirs)
    return {
        "alive_at_first_player_kill": {str(k): v for k, v in counts.items()},
        "player_team_alive": mine,
        "enemy_alive": enemy,
        "is_last_alive_on_team": mine == 1,
        "disadvantage": max(0, enemy - mine),
        "note": "counts read from the round's recorded alive samples",
    }


def build_dossier(*, timeline: dict, alignment: dict | None = None,
                  linkage: dict | None = None, state_rows: list[dict] | None = None,
                  source: str = "allstar_alignment",
                  tickrate: int | None = None) -> dict:
    """Assemble one moment dossier from timeline data (+ optional state)."""
    tickrate = int(tickrate or timeline.get("tickrate") or DEFAULT_TICKRATE)
    kill_ticks = [int(t) for t in ((alignment or {}).get("kill_ticks")
                                   or (linkage or {}).get("kill_ticks") or [])]
    start, end = _window(alignment, kill_ticks)
    round_no = None
    if alignment and alignment.get("round") is not None:
        round_no = int(alignment["round"])
    elif linkage and linkage.get("round") is not None:
        round_no = int(linkage["round"])
    steamid = str((alignment or {}).get("steamid")
                  or (linkage or {}).get("player_steamid") or "")

    people = _player_info(timeline)
    me = people.get(steamid, {})
    team_number = me.get("team_number")
    sides = timeline.get("sides_by_round") or {}
    side = team_label(team_number, sides, round_no)
    home, away = org_names((alignment or {}).get("demo")
                           or (linkage or {}).get("demo_path")
                           or timeline.get("demo_path"))
    team_slugs = {}
    if timeline.get("teams"):
        for number in timeline["teams"]:
            team_slugs[str(number)] = None

    round_info = None
    for row in timeline.get("rounds") or []:
        if round_no is not None and int(row.get("round") or 0) == round_no:
            round_info = row
            break

    availability: dict[str, bool] = {}
    unknowns = list(UNKNOWN_BASELINE)

    # ── chronology ────────────────────────────────────────────────────────
    kills_all = [k for k in timeline.get("kills_all") or []]
    timeline_kills = [k for k in kills_all
                      if round_no is not None and int(k.get("round") or 0) == round_no]
    if kill_ticks:
        pov_kills = [k for k in timeline_kills if int(k.get("tick")) in set(kill_ticks)]
    else:
        pov_kills = [k for k in timeline_kills
                     if str(k.get("attacker_steam_id")) == steamid
                     and _in_window(k.get("tick"), start, end)]

    chronology: list[dict] = []

    def add(tick, kind, **fields):
        entry = {"tick": int(tick), "t": tick_to_seconds(tick, tickrate, start),
                 "kind": kind}
        entry.update({k: v for k, v in fields.items() if v is not None})
        chronology.append(entry)

    for hurt in _raw_rows(timeline, "player_hurt"):
        tick = hurt.get("tick")
        if not _in_window(tick, start, end):
            continue
        add(tick, "damage",
            attacker=hurt.get("attacker_name"),
            victim=hurt.get("user_name"),
            weapon=hurt.get("weapon"),
            dmg_health=hurt.get("dmg_health"),
            dmg_armor=hurt.get("dmg_armor"),
            hitgroup=hurt.get("hitgroup"),
            victim_health_after=hurt.get("health"),
            victim_armor_after=hurt.get("armor"))
    for kw in [k for k in timeline_kills if _in_window(k.get("tick"), start, end)]:
        event = kw.get("kill_event") or {}
        add(kw.get("tick"), "kill",
            attacker=kw.get("attacker"),
            attacker_steam_id=kw.get("attacker_steam_id"),
            victim=kw.get("victim"),
            victim_steam_id=kw.get("victim_steam_id"),
            weapon=kw.get("weapon"),
            victim_weapon=kw.get("victim_weapon"),
            headshot=bool(kw.get("headshot")),
            penetrated=kw.get("penetrated"),
            is_bomb=bool(kw.get("is_bomb")),
            util_kill=bool(kw.get("util_kill")),
            util_dmg_before=kw.get("util_dmg_before"),
            blinded_by=kw.get("blinded_by") or None,
            noscope=event.get("noscope"),
            thrusmoke=event.get("thrusmoke"),
            attackerblind=event.get("attackerblind"),
            attackerinair=event.get("attackerinair"),
            assister=event.get("assister_name"),
            assister_steamid=event.get("assister_steamid"),
            distance=event.get("distance"),
            hitgroup=event.get("hitgroup"),
            dmg_health=event.get("dmg_health"),
            attacker_place=kw.get("attacker_place"),
            victim_place=kw.get("victim_place"),
            dominated=bool(event.get("dominated")),
            revenge=bool(event.get("revenge")))
    for fire in _raw_rows(timeline, "weapon_fire"):
        tick = fire.get("tick")
        if not _in_window(tick, start, end):
            continue
        if str(fire.get("user_steamid")) != steamid and \
                str(fire.get("user_steamid")) not in {str(k.get("victim_steam_id"))
                                                      for k in pov_kills}:
            continue
        add(tick, "shot", shooter=fire.get("user_name"),
            weapon=fire.get("weapon"), silenced=fire.get("silenced"))
    for throw in timeline.get("util_throws") or []:
        tick = throw.get("tick")
        if not _in_window(tick, start, end):
            continue
        add(tick, "utility_thrown", player=throw.get("player"),
            util=throw.get("util"), x=throw.get("x"), y=throw.get("y"))
    for dmg in timeline.get("util_damages") or []:
        tick = dmg.get("tick")
        if not _in_window(tick, start, end):
            continue
        add(tick, "utility_damage", attacker=dmg.get("attacker"),
            victim=dmg.get("victim"), weapon=dmg.get("weapon"), dmg=dmg.get("dmg"))
    for action in timeline.get("bomb_actions") or []:
        tick = action.get("tick")
        if not _in_window(tick, start, end):
            continue
        add(tick, "bomb", action=action.get("type"), player=action.get("player"),
            site=action.get("site"))

    chronology.sort(key=lambda entry: (entry["tick"], entry["kind"]))

    # ── pacing (measured, not inferred) ──────────────────────────────────
    pacing: dict = {}
    if start is not None and end is not None:
        pacing["window_seconds"] = tick_to_seconds(end - start, tickrate)
    if pov_kills:
        first, last = pov_kills[0]["tick"], pov_kills[-1]["tick"]
        pacing.update({
            "player_kills": len(pov_kills),
            "kill_span_seconds": tick_to_seconds(last - first, tickrate),
            "time_to_first_kill_seconds": tick_to_seconds(first - start, tickrate)
            if start is not None else None,
            "dead_time_after_last_kill_seconds": tick_to_seconds(end - last, tickrate)
            if end is not None else None,
            "kills_per_10s": (round(len(pov_kills) / max(
                tick_to_seconds(last - first, tickrate) or 0.1, 0.1) * 10.0, 3)
                if len(pov_kills) > 1 else None),
        })
    shots_by_player = sum(1 for e in chronology
                          if e["kind"] == "shot" and e.get("shooter")
                          == me.get("nick"))
    if shots_by_player:
        pacing["player_shots_in_window"] = shots_by_player
        if pov_kills:
            pacing["shots_per_kill"] = round(shots_by_player / len(pov_kills), 3)

    # ── state (positions/health) where genuinely available ───────────────
    state_summary = None
    if state_rows:
        rows = [r for r in state_rows
                if _in_window(_int_or_none(r.get("tick")), start, end)]
        mine = [r for r in rows if str(r.get("steamid")) == steamid]
        if mine:
            availability["player_state"] = True
            xs = [v for v in (_float_or_none(r.get("X")) for r in mine)
                  if v is not None]
            ys = [v for v in (_float_or_none(r.get("Y")) for r in mine)
                  if v is not None]
            zs = [v for v in (_float_or_none(r.get("Z")) for r in mine)
                  if v is not None]
            state_summary = {
                "samples": len(mine),
                "sample_interval_ticks": 8,
                "position_x_range": [round(min(xs), 1), round(max(xs), 1)] if xs else None,
                "position_y_range": [round(min(ys), 1), round(max(ys), 1)] if ys else None,
                "position_z_range": [round(min(zs), 1), round(max(zs), 1)] if zs else None,
                "health_min": _numeric_min(r.get("health") for r in mine),
                "health_max": _numeric_max(r.get("health") for r in mine),
                "yaw_degrees_range": _range([r.get("yaw") for r in mine]),
                "pitch_degrees_range": _range([r.get("pitch") for r in mine]),
                "weapon_indices": sorted({
                    idx for idx in (_int_or_none(r.get("m_iItemDefinitionIndex"))
                                    for r in mine) if idx is not None}),
                "snapshot_sources": sorted({str(r.get("_source") or "unknown")
                                            for r in mine}),
            }

    # ── round context ────────────────────────────────────────────────────
    # Canonical keys always exist (None when the timeline has no such round), so
    # consumers never have to guess whether a field is missing or null.
    round_context: dict = {
        "round": round_no, "start_tick": None, "freeze_end_tick": None,
        "end_tick": None, "duration_seconds": None, "winner_team": None,
        "win_reason": None, "score_after": None, "match_point_for": None,
        "overtime": None, "closes_map": None, "hot_zones": None,
        "alive_samples": 0, "stakes": None,
        "man_advantage_at_first_kill": None, "player_round_stats": None,
    }
    if round_info:
        round_context.update({
            "start_tick": round_info.get("start_tick"),
            "freeze_end_tick": round_info.get("freeze_end_tick"),
            "end_tick": round_info.get("end_tick"),
            "duration_seconds": tick_to_seconds(
                (round_info.get("end_tick") or 0) - (round_info.get("start_tick") or 0),
                tickrate) if round_info.get("end_tick") else None,
            "winner_team": round_info.get("winner_team"),
            "win_reason": round_info.get("win_reason"),
            "score_after": round_info.get("score_after"),
            "match_point_for": round_info.get("match_point_for"),
            "overtime": round_info.get("overtime"),
            "closes_map": round_info.get("closes_map"),
            "hot_zones": round_info.get("hot_zones"),
            "alive_samples": len(round_info.get("alive") or []),
        })
        stakes = round_info.get("stakes") or {}
        if stakes:
            availability["round_stakes"] = True
            round_context["stakes"] = {
                "buys": stakes.get("buys"),
                "equip_value": stakes.get("equip_value"),
                "win_streak_before": stakes.get("win_streak_before"),
                "deficit_at_start": stakes.get("deficit_at_start"),
                "is_pistol": stakes.get("is_pistol"),
                "buy_source": stakes.get("buy_source"),
            }
        round_context["man_advantage_at_first_kill"] = _clutch_context(
            round_info, team_number, pov_kills[0]["tick"] if pov_kills else None)
        player_round = None
        for entry in timeline.get("pros") or []:
            if str(entry.get("steam_id")) == steamid:
                for row in entry.get("rounds") or []:
                    if int(row.get("round") or 0) == (round_no or -1):
                        player_round = {k: v for k, v in row.items() if k != "round"}
        if player_round:
            round_context["player_round_stats"] = player_round

    availability.update({
        "chronology": bool(chronology),
        "kill_flags": any("headshot" in e for e in chronology if e["kind"] == "kill"),
        "round_stakes": "round_stakes" in availability,
        "player_state": state_summary is not None,
        "utility_events": any(e["kind"].startswith("utility") for e in chronology),
        "bomb_events": any(e["kind"] == "bomb" for e in chronology),
    })
    if state_summary is None:
        unknowns.append("player_positions_health_angles (no state snapshot for this "
                        "moment window)")

    dossier = {
        "dossier_version": DOSSIER_VERSION,
        "moment_id": None,  # filled by the caller
        "generated_at": iso(),
        "source": source,
        "provenance": {
            "demo_path": (alignment or {}).get("demo")
            or (linkage or {}).get("demo_path") or timeline.get("demo_path"),
            "match_id": (alignment or {}).get("match_id")
            or (linkage or {}).get("match_id"),
            "map": timeline.get("map") or (alignment or {}).get("map"),
            "tickrate": tickrate,
            "tickrate_source": ("timeline.tickrate" if timeline.get("tickrate")
                                else "cs2_default_64 (not declared in the timeline)"),
            "timeline_version": timeline.get("timeline_version"),
            "event_data_version": timeline.get("event_data_version"),
            "stakes_version": timeline.get("stakes_version"),
            "place_source": timeline.get("place_source"),
            "alignment_method": (alignment or {}).get("method"),
            "alignment_confidence": (alignment or {}).get("confidence"),
            "state_source": (sorted({str(r.get("_source"))
                                     for r in (state_rows or [])
                                     if r.get("_source")}) or ["none"])[0],
        },
        "bounds": {
            "kill_window": {"tick_start": start, "tick_end": end,
                            "seconds": tick_to_seconds((end or 0) - (start or 0),
                                                       tickrate)
                            if start is not None and end is not None else None,
                            "source": BOUNDS_SOURCE_INFERRED,
                            "kill_ticks": kill_ticks},
            "round": {"tick_start": (alignment or {}).get("round_tick_start"),
                      "tick_end": (alignment or {}).get("round_tick_end")},
            "published_clip_bounds": {
                "known": False,
                "reason": "the original AllStar clip's trim is not available from "
                          "the demo or the preserved payload",
            },
        },
        "clip_verification": {
            "verified": False,
            "basis": "map + player + round + kill-count alignment",
            "note": CLIP_VERIFICATION_NOTE,
            "allstar_clip_id": (alignment or {}).get("clip_id")
            or (linkage or {}).get("allstar_clip_id"),
            "title": (alignment or {}).get("title") or (linkage or {}).get("title"),
        },
        "identity": {
            "player_steam_id": steamid or None,
            "player_nick": (alignment or {}).get("player") or me.get("nick")
            or (linkage or {}).get("player_nick"),
            "team_number": team_number,
            "side": side,
            "team_org_slug": home,
            "opponent_org_slug": away,
            "is_pro": me.get("is_pro"),
            "teams_steamids": timeline.get("teams"),
        },
        "round_context": round_context,
        "pacing": pacing,
        "player_state": state_summary,
        "chronology": chronology,
        "availability": availability,
        "unknowns": unknowns,
        "counts": {
            "chronology_events": len(chronology),
            "kills_in_window": sum(1 for e in chronology if e["kind"] == "kill"),
            "player_kills_in_window": len(pov_kills),
            "damage_events": sum(1 for e in chronology if e["kind"] == "damage"),
            "shots": sum(1 for e in chronology if e["kind"] == "shot"),
        },
    }
    dossier["source_hash"] = dossier_source_hash(dossier)
    return dossier


def _int_or_none(value):
    """NaN/None-safe int. Parquet columns legitimately contain NaN."""
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if as_float != as_float:  # NaN
        return None
    return int(as_float)


def _float_or_none(value):
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return None if as_float != as_float else as_float


def _range(values) -> list | None:
    clean = [v for v in (_float_or_none(v) for v in values) if v is not None]
    if not clean:
        return None
    return [round(min(clean), 2), round(max(clean), 2)]


def _numeric_min(values):
    clean = [v for v in (_float_or_none(v) for v in values) if v is not None]
    return min(clean) if clean else None


def _numeric_max(values):
    clean = [v for v in (_float_or_none(v) for v in values) if v is not None]
    return max(clean) if clean else None


def dossier_source_hash(dossier: dict) -> str:
    """Hash of the identity + bounds + source versions (drives idempotency)."""
    return sha256_text(dumps({
        "demo": dossier["provenance"]["demo_path"],
        "map": dossier["provenance"]["map"],
        "round": dossier["round_context"].get("round"),
        "kill_ticks": dossier["bounds"]["kill_window"]["kill_ticks"],
        "player": dossier["identity"]["player_steam_id"],
        "timeline_version": dossier["provenance"]["timeline_version"],
        "event_data_version": dossier["provenance"]["event_data_version"],
        # State availability is part of the identity: adding a snapshot enriches
        # the dossier and must invalidate the previous file.
        "state_source": dossier["provenance"].get("state_source"),
        "dossier_version": dossier["dossier_version"],
    }))[:32]


def moment_id(dossier: dict) -> str:
    tick_start = dossier["bounds"]["kill_window"]["tick_start"]
    tick_end = dossier["bounds"]["kill_window"]["tick_end"]
    key = (f"{dossier['provenance']['match_id']}|"
           f"{dossier['provenance']['map']}|"
           f"{dossier['round_context'].get('round')}|"
           f"{dossier['identity']['player_steam_id']}|{tick_start}|{tick_end}|"
           f"{len(dossier['bounds']['kill_window']['kill_ticks'])}")
    return sha256_text(key)[:24]


def render_text(dossier: dict) -> str:
    """Compact prose/table rendering for an LLM to read directly."""
    prov = dossier["provenance"]
    ident = dossier["identity"]
    bounds = dossier["bounds"]
    rc = dossier["round_context"]
    lines = [
        f"# Moment {dossier['moment_id']} — {ident.get('player_nick')} "
        f"on {prov.get('map')} (round {rc.get('round')})",
        "",
        f"demo: {prov.get('demo_path')}",
        f"match_id: {prov.get('match_id')}   map: {prov.get('map')}   "
        f"tickrate: {prov.get('tickrate')} ({prov.get('tickrate_source')})",
        f"player: {ident.get('player_nick')} ({ident.get('player_steam_id')})  "
        f"side: {ident.get('side')}  team: {ident.get('team_org_slug')}  "
        f"opponent: {ident.get('opponent_org_slug')}",
        "",
        "## Bounds",
        f"kill window (INFERRED): ticks {bounds['kill_window']['tick_start']}"
        f"-{bounds['kill_window']['tick_end']} "
        f"({bounds['kill_window']['seconds']}s, "
        f"{bounds['kill_window']['source']})",
        f"kill ticks: {bounds['kill_window']['kill_ticks']}",
        f"round bounds: {bounds['round']['tick_start']}-{bounds['round']['tick_end']}",
        "published clip trim: UNKNOWN (not recoverable from a demo)",
        "",
        "## Round context",
    ]
    for key in ("winner_team", "win_reason", "score_after", "match_point_for",
                "overtime", "closes_map", "hot_zones", "duration_seconds"):
        if rc.get(key) is not None:
            lines.append(f"{key}: {rc[key]}")
    if rc.get("stakes"):
        lines.append(f"stakes: {rc['stakes']}")
    if rc.get("man_advantage_at_first_kill"):
        lines.append(f"man advantage at first kill: "
                     f"{rc['man_advantage_at_first_kill']}")
    if rc.get("player_round_stats"):
        lines.append(f"player round stats: {rc['player_round_stats']}")
    if dossier.get("pacing"):
        lines.append(f"pacing: {dossier['pacing']}")
    if dossier.get("player_state"):
        lines.append(f"player state: {dossier['player_state']}")
    lines += ["", "## Chronology (tick, +seconds from window start)"]
    for entry in dossier["chronology"]:
        kind = entry["kind"]
        detail = {k: v for k, v in entry.items() if k not in {"tick", "t", "kind"}}
        lines.append(f"  {entry['tick']:>7}  +{entry['t']:<7} {kind:<15} {detail}")
    lines += ["", "## Verification"]
    lines.append(f"clip verification: {dossier['clip_verification']['verified']} — "
                 f"{dossier['clip_verification']['note']}")
    lines.append("")
    lines.append("## Availability")
    for key, value in sorted(dossier["availability"].items()):
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("## Unknowns (not measured, not inferred)")
    for item in dossier["unknowns"]:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    """Temp file + replace: a reader never sees a half-written dossier."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_dossier(dossier: dict, *, root: Path, force: bool = False) -> tuple[str, str]:
    """Write JSON + text. Returns ('built'|'skipped_current', path)."""
    moment = dossier["moment_id"]
    json_path = Path(root) / f"{moment}.json"
    text_path = Path(root) / f"{moment}.txt"
    if json_path.is_file() and not force:
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("dossier_version") == dossier["dossier_version"] and \
                existing.get("source_hash") == dossier.get("source_hash"):
            return "skipped_current", str(json_path)
    write_atomic(json_path, json.dumps(dossier, indent=1, sort_keys=True,
                                       ensure_ascii=False))
    write_atomic(text_path, render_text(dossier))
    return "built", str(json_path)


def load_state_rows(clip_dir: Path) -> list[dict] | None:
    """Staged state.parquet rows, or a state snapshot produced by enrichment."""
    candidates = [Path(clip_dir) / "state.parquet",
                  Path(clip_dir) / "state_enriched.parquet"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import pandas as pd

            frame = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 - unreadable parquet = unavailable
            continue
        rows = frame.to_dict(orient="records")
        for row in rows:
            row["_source"] = path.name
        return rows
    return None
