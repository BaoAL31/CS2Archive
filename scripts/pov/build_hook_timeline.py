"""Build a Hook Timeline: 2-4 impressive POV moments for a video cold open.

The hook is a no-spoiler cold open prepended to a POV: rendered with
``cl_draw_only_deathnotices 1`` (full HUD hidden, killfeed kept — the Shorts
format), so no score/round/timer information leaks. Multiple moments are
allowed when several are impressive; the assembled hook is heavily edited
(tight kill-anchored trims + crossfades, climax last).

Detection is the Shorts extractor (``scripts/shorts/build_short_timeline.py``);
this module only ranks and selects. Tiers are an ordered, configurable list so
the qualification threshold can be tuned without a code change.

NOTE: the shorts extractor only emits multikills at >= 4 kills — there is no
3k/2k short, so a "first multikill" fallback tier cannot be filled today.

Usage:
    python scripts/pov/build_hook_timeline.py <demo.dem> --player <steam64|nick>
    python scripts/pov/build_hook_timeline.py <demo.dem> --player donk --max-moments 3
    python scripts/pov/build_hook_timeline.py <demo.dem> --player donk \
        --tiers clutch_1v5,clutch_1v4,clutch_1v3,5k,4k
    python scripts/pov/build_hook_timeline.py <demo.dem> --include-all-players

Output:
    renders/hook-{demo_stem}_{player}/hook_timeline.json
    (nothing is written when no moment qualifies — callers treat that as
    "no hook", not as an error)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.build_short_timeline import build_short_timeline  # noqa: E402

# Ordered tiers, best first. ``tier_rank`` is the position here.
#
# NOTE on perfect_shots: the Shorts extractor emits ``perfect_shots`` for
# 2-4 gun kills whose *shot count* ~= kill count (ammo efficiency), and it
# deliberately EXCLUDES a 1-bullet-per-kill 4K from the ``4k`` short. That is a
# different idea from the hook tier below: ``insta_kill`` is the LOS-based
# time-to-kill detector (scripts/overlay/victim_rewind.py) — victim visible for
# <= 0.5s before dying, high HP, not a trade. The ammo-efficiency tier is gone;
# a >=4-kill perfect_shots short is folded back into ``4k`` so those 4Ks do not
# silently disappear (see tier_of).
TIER_ORDER = (
    "clutch_1v5",
    "clutch_1v4",
    "clutch_1v3",
    "5k",
    "punch_up",
    "4k",
    "insta_kill",
    "clutch_attempt",
)

TIER_LABELS = {
    "clutch_1v5": "1v5 CLUTCH",
    "clutch_1v4": "1v4 CLUTCH",
    "clutch_1v3": "1v3 CLUTCH",
    "5k": "5K ACE",
    "punch_up": "PUNCH-UP 4K",
    "4k": "4K",
    "insta_kill": "INSTA KILL",
    "clutch_attempt": "1vX CLUTCH ATTEMPT",
}

DEFAULT_TIERS = ",".join(TIER_ORDER)

# Round 1 sits ~30s into the finished video, so replaying it as a cold open adds
# nothing the viewer is not about to see anyway. Round 0 is the knife/warmup
# round. Hooks therefore start at round 2 by default (``--min-round``).
MIN_ROUND_DEFAULT = 2


def round_allowed(round_no, min_round: int = MIN_ROUND_DEFAULT) -> bool:
    """False for the knife round, round 1, and anything unresolvable."""
    try:
        return int(round_no) >= int(min_round)
    except (TypeError, ValueError):
        return False


def timeline_matches(payload: dict, *, tiers: list[str] | None = None,
                     min_round: int | None = None,
                     max_moments: int | None = None,
                     max_seconds: float | None = None) -> bool:
    """True when a cached hook_timeline.json was built with the same filters.

    The pipeline reuses a cached timeline instead of re-running detection, so a
    stale cache would silently ignore a changed tier/threshold/round filter.
    Callers must rebuild when this returns False.
    """
    params = payload.get("params")
    if not isinstance(params, dict):
        return False  # pre-params cache: rebuild once
    if tiers is not None and list(params.get("tiers") or []) != list(tiers):
        return False
    if min_round is not None and int(params.get("min_round", 1)) != int(min_round):
        return False
    if max_moments is not None and int(params.get("max_moments", 0)) != int(max_moments):
        return False
    if max_seconds is not None and float(params.get("max_seconds", 0)) != float(max_seconds):
        return False
    return True


def _kills(short: dict) -> int:
    return len(short.get("kill_ticks") or [])


def tier_of(short: dict) -> str | None:
    """Best tier name for a detected short, or None if it does not qualify."""
    st = short.get("short_type")
    kills = _kills(short)
    if st == "clutch":
        cnt = (short.get("clutch_initial_count") or "").lower()
        name = f"clutch_{cnt}" if cnt in ("1v3", "1v4", "1v5") else None
        if name:
            return name
        return None
    if st == "clutch_attempt":
        return "clutch_attempt"
    if st == "perfect_shots":
        # Ammo-efficiency shorts are not a tier any more, but a >=4-kill one was
        # EXCLUDED from the 4k short by the detector — fold it back so the hook
        # still sees it. A sub-4-kill 4-tap is not hook material.
        return "4k" if kills >= 4 else None
    if kills >= 5:
        return "5k"
    if kills >= 4:
        return "punch_up" if short.get("punch_up_tags") else "4k"
    return None


def _rank_reason(tier: str, short: dict) -> str:
    kills = _kills(short)
    if tier.startswith("clutch_1v"):
        return f"{tier.replace('clutch_', '')} clutch won, {kills} kill(s)"
    if tier == "5k":
        return f"5k multikill, {kills} kill(s)"
    if tier == "punch_up":
        return f"punch-up {kills}k on lower-tier guns"
    if tier == "clutch_attempt":
        return f"1vX clutch attempt ({short.get('clutch_initial_count', '?')}), {kills} kill(s)"
    return f"{kills}k multikill"


def _map_mesh_available(map_name: str) -> bool:
    """True when the map collision mesh loads.

    ``closest_hit`` returns None BOTH for "no obstruction" and for "map mesh
    unavailable", and victim_rewind maps None to "LOS open". A missing mesh
    therefore reads as "LOS was always open", which pushes every TTK past the
    0.5s cap and silently yields zero insta kills. Callers must check this
    before believing a zero result.
    """
    if not map_name:
        return False
    try:
        from config import settings
        root = str(Path(settings.cs2util_root))
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.render.map_collision import _grid_for_map
        return _grid_for_map(map_name) is not None
    except Exception:
        return False


def insta_kill_candidates(demo_path: Path, player_sid: str, map_name: str,
                          tickrate: int, enabled: list[str]) -> list[dict]:
    """LOS-TTK insta kills for the POV player (victim_rewind detector).

    A kill qualifies when the victim was visible to the attacker for <= 0.5s
    before dying, was not already damaged (HP >= 90), and the kill was not a
    trade. Single-kill moments — that is the point: they are invisible to the
    multikill-based Shorts extractor.
    """
    if "insta_kill" not in enabled or not player_sid:
        return []
    if not _map_mesh_available(map_name):
        print(f"  [WARN] collision mesh unavailable for {map_name or '?'} — "
              f"skipping the insta_kill tier (LOS TTK cannot be trusted)")
        return []
    try:
        from overlay.victim_rewind import detect_from_demo
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] victim_rewind unavailable ({e}) — no insta_kill tier")
        return []

    try:
        rows = detect_from_demo(demo_path, player=str(player_sid))
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] insta_kill detection failed ({e})")
        return []

    out: list[dict] = []
    for r in rows:
        if "insta_kill" not in (r.get("reasons") or []):
            continue
        tick = int(r["kill_tick"])
        los = r.get("los_open_tick")
        ttk = (tick - int(los)) / tickrate if los else None
        pre = int(2.5 * tickrate)
        post = int(2.0 * tickrate)
        label = "INSTA KILL"
        if ttk is not None:
            label = f"INSTA {ttk:.2f}s"
        if (r.get("hitgroup") or "").lower() == "head":
            label += " HEAD"
        out.append({
            "tier": "insta_kill",
            "tier_rank": enabled.index("insta_kill"),
            "label": label,
            "short_type": "insta_kill",
            "round": int(r.get("round") or 0),
            "pov_steam_id": str(r.get("attacker_sid") or player_sid),
            "pov_nick": None,
            "start_tick": tick - pre,
            "end_tick": tick + post,
            "kill_ticks": [tick],
            "round_win_tick": None,
            "clutch_initial_count": "",
            "rank_reason": (f"insta kill: {r.get('weapon') or '?'} "
                            f"{(r.get('hitgroup') or '').strip()}, "
                            f"LOS->kill {int(ttk * 1000)}ms" if ttk is not None
                            else f"insta kill: {r.get('weapon') or '?'}"),
            "ttk": ttk,
        })
    # Fastest reaction first when several are available.
    out.sort(key=lambda m: (m["ttk"] if m["ttk"] is not None else 99.0,
                            m["start_tick"]))
    return out


def _round_for_tick(kills: list[dict], tick: int) -> int | None:
    """Round of the kill nearest ``tick`` (the timeline's kill list carries it)."""
    best, best_d = None, None
    for k in kills:
        d = abs(int(k.get("tick", 0)) - tick)
        if best_d is None or d < best_d:
            best, best_d = int(k.get("round", 0)), d
    return best


def _overlaps(a: dict, b: dict, slack: int = 64) -> bool:
    return not (a["end_tick"] + slack < b["start_tick"]
                or b["end_tick"] + slack < a["start_tick"])


def _moment(short: dict, tier: str, tier_rank: int, timeline: dict,
            player_nick: str) -> dict:
    kill_ticks = [int(k) for k in short.get("kill_ticks") or []]
    start_tick = int(short["start_tick"])
    end_tick = int(short["end_tick"])
    rnd = _round_for_tick(timeline.get("kills") or [], kill_ticks[0] if kill_ticks else start_tick)
    return {
        "tier": tier,
        "tier_rank": tier_rank,
        "label": TIER_LABELS.get(tier, tier.upper()),
        "short_type": short.get("short_type"),
        "round": rnd,
        "pov_steam_id": str(short.get("pov_steam_id", "")),
        "pov_nick": short.get("pov_nick") or player_nick,
        "start_tick": start_tick,
        "end_tick": end_tick,
        "kill_ticks": [t for t in kill_ticks if start_tick <= t <= end_tick],
        "round_win_tick": short.get("round_win_tick"),
        "clutch_initial_count": short.get("clutch_initial_count", ""),
        "pov_switch_tick": short.get("pov_switch_tick"),
        "pov_switch_to": short.get("pov_switch_to"),
        "pov_switch_to_nick": short.get("pov_switch_to_nick"),
        "rank_reason": _rank_reason(tier, short),
    }


def _resolve_player_sid(player: str | None) -> str | None:
    """steam64 passthrough, or nickname -> steam_id via player_accounts.json."""
    if not player:
        return None
    who = str(player).strip()
    if who.isdigit() and len(who) >= 17:
        return who
    try:
        accounts = json.loads(
            (_PROJECT_ROOT / ".data" / "player_accounts.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    for a in accounts:
        nick = (a.get("nickname") or a.get("faceit_nickname") or "").strip().lower()
        if nick and nick == who.lower():
            return str(a.get("steam_id") or "") or None
    return None


def _sort_key(m: dict):
    """Tier first, then kill count desc, then faster TTK, then tick."""
    return (m["tier_rank"], -len(m.get("kill_ticks") or []),
            m.get("ttk") if m.get("ttk") is not None else 99.0,
            m["start_tick"])


def build_hook_timeline(
    demo_path: Path,
    player: str | None = None,
    pros_only: bool = True,
    tiers: str = DEFAULT_TIERS,
    max_moments: int = 3,
    max_seconds: float = 30.0,
    min_round: int = MIN_ROUND_DEFAULT,
    tickrate: int = 64,
) -> dict:
    """Rank a demo's hook-worthy moments and pick up to ``max_moments``.

    ``player`` (steam64 or nickname) restricts to one POV — the POV of the
    video the hook is prepended to. Selection is best-first by tier, one moment
    per round, then re-ordered **climax last** for the edit.
    """
    demo_path = Path(demo_path)
    enabled = [t.strip() for t in tiers.split(",") if t.strip() in TIER_ORDER]
    if not enabled:
        raise ValueError(f"no valid tiers in {tiers!r} (known: {', '.join(TIER_ORDER)})")

    timeline = build_short_timeline(demo_path, pros_only=pros_only, player=player)
    shorts = timeline.get("shorts") or []

    want = None
    if player:
        want = str(player).strip().lower()

    candidates: list[dict] = []
    for s in shorts:
        if want:
            sid = str(s.get("pov_steam_id", "")).lower()
            nick = (s.get("pov_nick") or "").strip().lower()
            if want not in (sid, nick):
                continue
        tier = tier_of(s)
        if tier is None or tier not in enabled:
            continue
        nick = s.get("pov_nick") or (player or "Unknown")
        candidates.append(_moment(s, tier, enabled.index(tier), timeline, nick))

    # LOS-TTK insta kills: single-kill moments the multikill extractor cannot
    # see (e.g. a 0.19s pistol headshot). Only the POV player's own kills.
    sid = _resolve_player_sid(player)
    map_name = str(timeline.get("map") or "")
    if "insta_kill" in enabled and sid:
        insta = insta_kill_candidates(demo_path, sid, map_name, tickrate, enabled)
        for m in insta:
            m["pov_nick"] = m.get("pov_nick") or (player or "Unknown")
        candidates.extend(insta)

    # Round filter: a hook must not replay the opening round (see round_allowed).
    before = len(candidates)
    candidates = [c for c in candidates if round_allowed(c.get("round"), min_round)]
    dropped_early = before - len(candidates)
    if dropped_early:
        print(f"  [round] dropped {dropped_early} moment(s) from rounds < {min_round}")

    # Best first, then kills desc.
    candidates.sort(key=_sort_key)

    picked: list[dict] = []
    for c in candidates:
        if any(_overlaps(c, p) for p in picked):
            continue
        picked.append(c)
        if len(picked) >= max_moments:
            break

    # Budget guard: drop the weakest moments until the (uncut) footage fits.
    while len(picked) > 1:
        total = sum((m["end_tick"] - m["start_tick"]) / tickrate for m in picked)
        if total <= max_seconds:
            break
        picked.pop()

    # Climax last (the assembly keeps this order).
    picked = list(reversed(picked))

    player_sid = picked[0]["pov_steam_id"] if picked else (player or "")
    player_nick = (picked[0].get("pov_nick") if picked else None) or (player or "Unknown")
    for m in picked:
        if not m.get("pov_nick"):
            m["pov_nick"] = player_nick
    return {
        "hook_type": "hook_timeline",
        "demo_path": str(demo_path),
        "map": timeline.get("map", "Unknown"),
        "tickrate": int(timeline.get("tickrate") or tickrate),
        "player": {"steam_id": player_sid, "nick": player_nick},
        "params": {
            "tiers": enabled,
            "min_round": int(min_round),
            "max_moments": int(max_moments),
            "max_seconds": float(max_seconds),
        },
        "picked": picked,
        "candidates": candidates,
        "reason": ("; ".join(m["rank_reason"] for m in reversed(picked))
                   if picked else
                   (f"no qualifying moment above round {min_round - 1}" if dropped_early
                    else "no qualifying moment")),
    }


def hook_run_dir(demo_path: Path, player: str | None) -> Path:
    who = (player or "all").strip()
    return _PROJECT_ROOT / "renders" / f"hook-{Path(demo_path).stem}_{who}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a Hook Timeline JSON from a demo")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument("--player", default=None,
                    help="POV player to build the hook for (steam64 or nickname)")
    ap.add_argument("--tiers", default=DEFAULT_TIERS,
                    help=f"Comma-separated qualifying tiers, best first "
                         f"(default: {DEFAULT_TIERS})")
    ap.add_argument("--max-moments", type=int, default=3,
                    help="Keep at most this many moments (default: 3)")
    ap.add_argument("--max-seconds", type=float, default=30.0,
                    help="Total uncut footage budget in seconds (default: 30)")
    ap.add_argument("--min-round", type=int, default=MIN_ROUND_DEFAULT,
                    help=f"Earliest round a hook may use (default: {MIN_ROUND_DEFAULT} — "
                         f"round 1 is ~30s into the video, so replaying it "
                         f"as a cold open is wasted; 0/1 are always useless)")
    ap.add_argument("--include-all-players", action="store_true",
                    help="Accept moments from any player (default: Recognised Pros only; "
                         "ignored when --player is given)")
    ap.add_argument("--output", "-o", type=Path, default=None,
                    help="Override the output JSON path")
    args = ap.parse_args()

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    timeline = build_hook_timeline(
        demo,
        player=args.player,
        pros_only=not args.include_all_players,
        tiers=args.tiers,
        max_moments=args.max_moments,
        max_seconds=args.max_seconds,
        min_round=args.min_round,
    )

    if not timeline["picked"]:
        print(f"[OK] no hook: {timeline['reason']} (no output written)")
        return 0

    out = args.output or (hook_run_dir(demo, args.player) / "hook_timeline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")

    print(f"[OK] {len(timeline['picked'])} moment(s) for "
          f"{timeline['player']['nick']} | {timeline['map']} -> {out}")
    for i, m in enumerate(timeline["picked"], 1):
        print(f"  {i}. {m['label']:22s} r{m['round']} "
              f"ticks {m['start_tick']}->{m['end_tick']} "
              f"({len(m['kill_ticks'])} kill(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
