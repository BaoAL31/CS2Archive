"""Build the Allstar nominations file the picker agent reads.

The picker must not be advantaged over a human reviewer: it gets the same demo
evidence used to judge the clips — kill timings, weapons, headshots, buy state,
round result — extracted from the action timeline, not summarised by us. Clips
whose demo is not local get ``demo_facts: null`` and the prompt tells the agent
to say so rather than guess.

Two payload rules, both learned the hard way:

* **Compact JSON.** The pretty-printed file was 167 KB and the slowest model
  spent its whole budget paging through it 1100 lines at a time instead of
  researching. Compact is 92 KB and reads in one or two calls.
* **Only the fields the prompt names.** ``alive_counts_over_round`` and
  ``hot_zones`` were 10 KB of per-round arrays the prompt never mentions.

Usage:
    python -m cs2archive.shorts.picker_nominations --days 7 --limit 5
    python -m cs2archive.shorts.picker_nominations --out .data/allstar_picker/noms.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from cs2archive.shorts.allstar_pool import (
    DEFAULT_DAYS, EVIDENCE_DB, PROBE, SNAPSHOTS, eligible_clips,
)

ROOT = Path(__file__).resolve().parents[2]
PICKER_DIR = ROOT / ".data" / "allstar_picker"
TIMELINES = PICKER_DIR / "timelines"

# Fields the prompt never references. Dropping them costs the agent nothing and
# keeps the payload small enough to read in one pass.
_DROP_FACTS = ("alive_counts_over_round", "hot_zones", "equip_players",
               "round_winner_team_id")


def _rel(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (tests, external demos)."""
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)


def demo_folders(root: Path | None = None) -> dict[str, Path]:
    """match_id -> local demo folder. Factual evidence, not a guess."""
    out: dict[str, Path] = {}
    base = Path(root) if root is not None else ROOT / "demos" / "hltv"
    if not base.is_dir():
        return out
    for child in base.iterdir():
        if child.is_dir():
            out.setdefault(child.name.split("-", 1)[0], child)
    return out


def timeline_for(demo: Path, cache: Path = TIMELINES) -> dict | None:
    """Action timeline for one demo, cached. None when the demo won't parse."""
    from cs2archive.highlights import build_action_timeline as atl

    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{demo.parent.name}__{demo.stem}.json"
    try:
        path = atl.ensure_action_timeline(demo, output=out)
    except Exception as exc:  # noqa: BLE001 - a bad demo must not kill the run
        print(f"[warn] timeline failed for {demo.name}: {exc}", flush=True)
        return None
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def series_summary(folder: Path, my_team: str | None,
                   cache: Path = TIMELINES) -> list[dict]:
    """Every map of the series, so the match result never depends on a web summary."""
    rows = []
    for demo in sorted(folder.glob("*.dem")):
        timeline = timeline_for(demo, cache)
        if not timeline or not timeline.get("rounds"):
            continue
        last = timeline["rounds"][-1]
        score = last.get("score_after") or {}
        winner = last.get("winner_team")
        mine = score.get(str(my_team)) if my_team is not None else None
        theirs = [v for k, v in score.items() if str(k) != str(my_team)]
        rows.append({
            "map": timeline.get("map"),
            "score": score,
            "winner_team_id": winner,
            "player_team_won_map": (str(winner) == str(my_team)) if winner is not None else None,
            "player_team_rounds": mine,
            "opponent_rounds": theirs[0] if theirs else None,
            "demo": _rel(demo),
        })
    return rows


def _kill_row(kill: dict, first_tick: int, tps: int) -> dict:
    event = kill.get("kill_event") or {}
    return {
        "tick": int(kill["tick"]),
        "t_after_first_s": round((int(kill["tick"]) - first_tick) / tps, 2),
        "victim": kill.get("victim"),
        "weapon": kill.get("weapon"),
        "victim_weapon": kill.get("victim_weapon"),
        "headshot": bool(event.get("headshot")),
        "hitgroup": event.get("hitgroup"),
        "damage": event.get("dmg_health"),
        "distance_m": round(float(event.get("distance") or 0), 1),
        "through_smoke": bool(event.get("thrusmoke")),
        "penetrated": event.get("penetrated") or 0,
        "attacker_place": kill.get("attacker_place"),
        "victim_place": kill.get("victim_place"),
    }


def facts_for(clip: dict, folder: Path | None, cache: Path = TIMELINES,
              tps: int = 64) -> dict | None:
    """Demo facts for one clip, or an ``error`` marker, or None when not local."""
    if folder is None:
        return None
    sid = str(clip.get("steamid") or "")
    rnd = clip.get("round")
    hits = []
    for demo in sorted(folder.glob("*.dem")):
        timeline = timeline_for(demo, cache)
        if not timeline:
            continue
        roster = {str(s) for members in (timeline.get("teams") or {}).values() for s in members}
        if sid not in roster:
            continue
        kills = [k for k in timeline.get("kills_all") or []
                 if str(k.get("attacker_steam_id")) == sid and k.get("round") == rnd]
        if kills:
            hits.append((demo, timeline, sorted(kills, key=lambda k: int(k["tick"]))))
    # A series has several maps: the same player/round number can appear in
    # more than one demo, so pin the clip's map before declaring ambiguity.
    want_map = str(clip.get("map") or "").replace("de_", "").lower()
    if want_map and len(hits) > 1:
        same = [h for h in hits
                if str(h[1].get("map") or "").replace("de_", "").lower() == want_map]
        if len(same) == 1:
            hits = same
    if len(hits) != 1:
        return {"error": f"moment found in {len(hits)} demos" if hits else "moment not found"}
    demo, timeline, kills = hits[0]

    rounds = {r["round"]: r for r in timeline.get("rounds") or []}
    round_row = rounds.get(rnd) or {}
    stakes = round_row.get("stakes") or {}
    my_team = next((t for t, members in (timeline.get("teams") or {}).items()
                    if sid in {str(m) for m in members}), None)
    winner = round_row.get("winner_team")
    ticks = [int(k["tick"]) for k in kills]
    span = (ticks[-1] - ticks[0]) / tps if len(ticks) > 1 else 0.0
    gaps = [round((b - a) / tps, 2) for a, b in zip(ticks, ticks[1:])]
    round_kills = [k for k in timeline.get("kills_all") or [] if k.get("round") == rnd]

    facts = {
        "demo": _rel(demo),
        "series": series_summary(folder, my_team, cache),
        "map": timeline.get("map"),
        "player_team": my_team,
        "round": rnd,
        "round_won_by_player_team": (str(winner) == str(my_team)) if winner is not None else None,
        "round_winner_team_id": winner,
        "score_after_round": round_row.get("score_after"),
        "win_reason": round_row.get("win_reason"),
        "buy": stakes.get("buys"),
        "equip_value": stakes.get("equip_value"),
        "equip_players": stakes.get("equip_players"),
        "is_pistol": stakes.get("is_pistol"),
        "deficit_at_start": stakes.get("deficit_at_start"),
        "alive_counts_over_round": round_row.get("alive"),
        "hot_zones": round_row.get("hot_zones"),
        "player_kills_in_round": len(kills),
        "all_kills_in_round": len(round_kills),
        "kill_span_seconds": round(span, 2),
        "gaps_seconds": gaps,
        "headshots": sum(1 for k in kills if (k.get("kill_event") or {}).get("headshot")),
        "kills": [_kill_row(k, ticks[0], tps) for k in kills],
        "other_kills_in_round": [
            {"attacker": k.get("attacker"), "victim": k.get("victim"),
             "weapon": k.get("weapon"), "tick": int(k["tick"])}
            for k in round_kills
            if str(k.get("attacker_steam_id")) != sid
        ],
    }
    for key in _DROP_FACTS:
        facts.pop(key, None)
    return facts


def facts_summary(facts: dict | None) -> dict | None:
    """The ranking-relevant numbers, small enough to sit in the index.

    The full per-kill detail lives in ``facts/<clip_id>.json``. This is what a
    reviewer needs to decide whether a clip is worth opening at all.
    """
    if not facts:
        return None
    if facts.get("error"):
        return {"error": facts["error"]}
    kills = facts.get("kills") or []
    weapons = sorted({str(k.get("weapon")) for k in kills if k.get("weapon")})
    victim_weapons = sorted({str(k.get("victim_weapon")) for k in kills if k.get("victim_weapon")})
    series = facts.get("series") or []
    return {
        "player_kills_in_round": facts.get("player_kills_in_round"),
        "all_kills_in_round": facts.get("all_kills_in_round"),
        "headshots": facts.get("headshots"),
        "kill_span_seconds": facts.get("kill_span_seconds"),
        "gaps_seconds": facts.get("gaps_seconds"),
        "buy": facts.get("buy"),
        "is_pistol": facts.get("is_pistol"),
        "deficit_at_start": facts.get("deficit_at_start"),
        "round_won_by_player_team": facts.get("round_won_by_player_team"),
        "score_after_round": facts.get("score_after_round"),
        "win_reason": facts.get("win_reason"),
        "weapons": weapons,
        "victim_weapons": victim_weapons,
        "through_smoke": sum(1 for k in kills if k.get("through_smoke")),
        "wallbangs": sum(1 for k in kills if k.get("penetrated")),
        "max_distance_m": max((k.get("distance_m") or 0) for k in kills) if kills else None,
        "series": [
            {"map": row.get("map"), "score": row.get("score"),
             "player_team_won_map": row.get("player_team_won_map")}
            for row in series
        ],
    }


def build_nominations(
    days: int = DEFAULT_DAYS,
    limit: int = 5,
    *,
    out: Path = PROBE,
    snapshots: Path = SNAPSHOTS,
    db: Path = EVIDENCE_DB,
    cache: Path = TIMELINES,
    demos_root: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Eligible clips, top ``limit`` by views, each with a demo-facts summary.

    The full facts are attached under ``facts`` on each nomination so
    :func:`write_nominations` can split them into their own files.
    """
    now = now or datetime.now(timezone.utc)
    clips, pool_stats = eligible_clips(days, out=out, snapshots=snapshots, db=db, now=now)
    folders = demo_folders(demos_root)
    shown = clips[:limit] if limit else clips

    nominations = []
    with_facts = 0
    for clip in shown:
        folder = folders.get(str(clip.get("match_id") or ""))
        facts = facts_for(clip, folder, cache)
        if facts and not facts.get("error"):
            with_facts += 1
        nominations.append({
            **clip,
            "local_demo_folder": folder.name if folder else None,
            "facts_summary": facts_summary(facts),
            "facts": facts,
        })

    return {
        "window": {
            "days": days,
            "generated_at": now.isoformat(),
            "oldest_match_date": min(
                (c["match_date"] for c in clips if c.get("match_date")), default=None),
        },
        "provenance": {
            "source": _rel(out),
            "snapshots": _rel(snapshots),
            "pool": pool_stats,
            "demo_facts": (
                "facts_summary is in this file; the full per-kill detail for each "
                "clip is in facts/<clip_id>.json. Both are extracted from the "
                "local .dem via build_action_timeline: per-kill tick/weapon/victim "
                "weapon/headshot/hitgroup/damage/distance/through-smoke, plus round "
                "buy type, equip values, score and winner. null = demo not local. "
                "This is the same evidence a human reviewer uses."
            ),
            "views": (
                "views is the newest Allstar snapshot. views_per_hour is lifetime "
                "views over the clip's real age (Allstar's createdDate), so it is "
                "age-normalised and comparable across clips. recent_views_per_hour "
                "is growth between snapshots and is null until two snapshots are "
                "at least 6h apart."
            ),
        },
        "total_eligible": len(clips),
        "shown": len(nominations),
        "with_demo_facts": with_facts,
        "nominations": nominations,
    }


def write_nominations(payload: dict, path: Path, *, facts_dir: Path | None = None) -> dict:
    """Write the index plus one facts file per clip.

    Two files, not one, because the agent's ``read`` tool refuses any line over
    50 KB: a single compact JSON file is one 70 KB line and is unreadable, while
    a single pretty-printed file is 167 KB and the agent spends its whole budget
    paging through it. The index is small and pageable; the per-clip facts are
    opened only for the clips actually being weighed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    facts_dir = Path(facts_dir) if facts_dir else path.parent / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    index = {**payload, "nominations": []}
    written = 0
    for nomination in payload.get("nominations") or []:
        facts = nomination.get("facts")
        entry = {k: v for k, v in nomination.items() if k != "facts"}
        clip_id = str(nomination.get("clip_id") or "")
        if facts is not None and clip_id:
            facts_path = facts_dir / f"{clip_id}.json"
            facts_path.write_text(json.dumps(facts, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            entry["facts_file"] = _rel(facts_path)
            written += 1
        index["nominations"].append(entry)

    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"index": str(path), "facts_files": written, "facts_dir": str(facts_dir)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Allstar picker nominations file")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--limit", type=int, default=5,
                    help="Shortlist size: top N eligible clips by views (default 5)")
    ap.add_argument("--out", type=Path, default=None, help="Nominations index to write")
    ap.add_argument("--facts-dir", type=Path, default=None,
                    help="Per-clip facts directory (default: <out>/../facts)")
    ap.add_argument("--probe", type=Path, default=PROBE)
    ap.add_argument("--snapshots", type=Path, default=SNAPSHOTS)
    ap.add_argument("--db", type=Path, default=EVIDENCE_DB)
    ap.add_argument("--cache", type=Path, default=TIMELINES)
    args = ap.parse_args()

    payload = build_nominations(args.days, args.limit, out=args.probe,
                                snapshots=args.snapshots, db=args.db, cache=args.cache)
    stamp = datetime.now(timezone.utc).date().isoformat()
    dest = args.out or (PICKER_DIR / f"nominations_{stamp}.json")
    written = write_nominations(payload, dest, facts_dir=args.facts_dir)
    size = dest.stat().st_size
    print(f"{written['index']}  {size} bytes")
    print(f"{written['facts_files']} facts files in {written['facts_dir']}")
    print(f"{payload['total_eligible']} eligible, {payload['shown']} shown, "
          f"{payload['with_demo_facts']} with demo facts")
    print(json.dumps(payload["provenance"]["pool"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
