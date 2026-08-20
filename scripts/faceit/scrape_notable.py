"""
FACEIT "notable matches" scrape — reports BOTH:

  A) Multi-pro matches: >= N known pros in the same match.
  B) Single-pro standout performances: a Recognised Pro with a notable
     individual game (K/D, ADR, or kills above threshold).

Stats are fetched once per match (deduped cache). No per-player ELO lookups,
so it finishes quickly instead of timing out like `faceit recent`.

Usage:
    python scripts/faceit/scrape_notable.py [--hours 168] [--count 25]
        [--min-pros 2] [--top 20]
        [--perf-kd 1.5] [--perf-adr 100] [--perf-kills 30] [--perf-limit 120]
        [--exclude-today] [--today-only]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure
ensure()

from player_accounts import list_accounts  # noqa: E402
from scrapers.faceit import FACEITClient  # noqa: E402
from faceit_names import faceit_nick  # noqa: E402


def pro_nicks() -> list[str]:
    return [a.nickname for a in list_accounts() if a.nickname]


def _num(val, cast):
    try:
        return cast(val)
    except (TypeError, ValueError):
        return 0


def _is_notable_perf(line: dict, kd_min: float, adr_min: float, kills_min: int) -> bool:
    kd = _num(line.get("kd"), float)
    adr = _num(line.get("adr"), float)
    kills = _num(line.get("kills"), int)
    return kd >= kd_min or adr >= adr_min or kills >= kills_min


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--count", type=int, default=25)
    ap.add_argument("--min-pros", type=int, default=2)
    ap.add_argument("--top", type=int, default=20,
                    help="Detailed stats for the N most-notable multi-pro matches "
                         "(most pros, then most recent).")
    ap.add_argument("--perf-kd", type=float, default=1.5,
                    help="Single-pro K/D threshold for a notable performance.")
    ap.add_argument("--perf-adr", type=float, default=100.0,
                    help="Single-pro ADR threshold for a notable performance.")
    ap.add_argument("--perf-kills", type=int, default=30,
                    help="Single-pro kills threshold for a notable performance.")
    ap.add_argument("--perf-limit", type=int, default=120,
                    help="Max number of single-pro matches to evaluate for performance.")
    args = ap.parse_args()

    pros = pro_nicks()
    if not pros:
        print("No Recognised Pros in .data/player_accounts.json.")
        return

    client = FACEITClient()
    today = datetime.now().date()
    try:
        match_pros: dict[str, set] = {}
        match_meta: dict[str, dict] = {}
        cutoff = datetime.now() - timedelta(hours=args.hours)

        for nick in pros:
            q = faceit_nick(nick)
            pid = await client.get_player_id(q)
            if not pid:
                continue
            matches = await client.get_player_matches(pid, limit=args.count)
            for m in matches:
                if m.date and m.date < cutoff:
                    continue
                match_pros.setdefault(m.match_id, set()).add(nick)
                match_meta.setdefault(m.match_id, {
                    "id": m.match_id, "date": m.date,
                    "team1": m.team1, "team2": m.team2,
                })

        # ---- Section A: multi-pro matches ----
        multi = {mid: ps for mid, ps in match_pros.items() if len(ps) >= args.min_pros}

        # ---- Section B: single-pro standout performances ----
        single = {mid: ps for mid, ps in match_pros.items()
                  if len(ps) == 1 and mid not in multi}

        # fetch stats once per match (deduped across both sections)
        stats_cache: dict[str, dict] = {}

        async def get_stats(mid: str):
            if mid not in stats_cache:
                stats_cache[mid] = await client.get_match_stats(mid) or {}
            return stats_cache[mid]

        performances = []
        single_ids = list(single.keys())[: args.perf_limit]
        for mid in single_ids:
            stats = await get_stats(mid)
            if not stats:
                continue
            pro = next(iter(single[mid]))
            line = stats.get("players", {}).get(pro)
            if not line:
                continue
            if _is_notable_perf(line, args.perf_kd, args.perf_adr, args.perf_kills):
                meta = match_meta.get(mid, {})
                performances.append({
                    "id": mid, "pro": pro, "line": line,
                    "map": stats.get("map", "?"),
                    "score": stats.get("score", ""),
                    "date": meta.get("date"),
                    "team1": meta.get("team1", "?"),
                    "team2": meta.get("team2", "?"),
                })

        # pre-fetch stats for the top multi-pro matches we'll detail
        ranked_multi = sorted(
            multi.items(),
            key=lambda x: (-len(x[1]),
                           -(match_meta.get(x[0], {}).get("date") or datetime.min).timestamp()),
        )
        for mid, _ in ranked_multi[: args.top]:
            await get_stats(mid)

        # average lobby ELO for a match (all players in stats; cached per player)
        _elo_sem = asyncio.Semaphore(8)

        async def lobby_elos(mid: str):
            stats = stats_cache.get(mid, {})
            pids = [p.get("player_id") for p in stats.get("players", {}).values()
                    if p.get("player_id")]
            async with _elo_sem:
                els = await asyncio.gather(*[client.get_player_elo(pid) for pid in pids])
            vals = [e for e in els if e is not None]
            avg = round(sum(vals) / len(vals)) if vals else None
            return avg, len(vals), len(pids)

        # ============ REPORT ============
        print(f"\n=== FACEIT NOTABLE MATCHES — last {args.hours}h "
              f"(multi-pro + single standout) ===")

        print(f"\n--- A) MULTI-PRO MATCHES (>= {args.min_pros} pros): {len(multi)} found "
              f"(showing top {min(args.top, len(multi))}) ---\n")
        for mid, ps in ranked_multi[: args.top]:
            stats = stats_cache.get(mid, {})
            meta = match_meta.get(mid, {})
            date = meta.get("date")
            datestr = date.strftime("%Y-%m-%d") if date else "?"
            mp = stats.get("map", "?")
            score = stats.get("score", "")
            print(f"* {mid}  ({len(ps)} pros)  {mp}  {score}  {datestr}")
            print(f"    teams: {meta.get('team1', '?')} vs {meta.get('team2', '?')}")
            print(f"    pros:  {', '.join(sorted(ps))}")
            if stats:
                for p in sorted(ps):
                    line = stats.get("players", {}).get(p)
                    if line:
                        print(f"      - {p}: {line.get('kills')}/{line.get('deaths')} "
                              f"(K/D {line.get('kd')}, ADR {line.get('adr')}, HS% {line.get('hs')})")
            avg, n, tot = await lobby_elos(mid)
            print(f"    avg lobby ELO: {avg} ({n}/{tot} players)" if avg is not None
                  else "    avg lobby ELO: n/a")
            print()

        print(f"--- B) SINGLE-PRO STANDOUT PERFORMANCES "
              f"(K/D>={args.perf_kd} or ADR>={args.perf_adr} or kills>={args.perf_kills}): "
              f"{len(performances)} found "
              f"(evaluated {len(single_ids)}/{len(single)} single-pro matches) ---\n")
        # rank by K/D then ADR
        performances.sort(key=lambda r: (-_num(r['line'].get('kd'), float),
                                         -_num(r['line'].get('adr'), float)))
        for r in performances:
            line = r["line"]
            datestr = r["date"].strftime("%Y-%m-%d") if r["date"] else "?"
            print(f"* {r['id']}  {r['map']}  {r['score']}  {datestr}")
            print(f"    teams: {r['team1']} vs {r['team2']}")
            print(f"    {r['pro']}: {line.get('kills')}/{line.get('deaths')} "
                  f"(K/D {line.get('kd')}, ADR {line.get('adr')}, HS% {line.get('hs')})")
            avg, n, tot = await lobby_elos(r["id"])
            print(f"    avg lobby ELO: {avg} ({n}/{tot} players)" if avg is not None
                  else "    avg lobby ELO: n/a")
            print()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
