"""
FACEIT "notable matches" scrape — reports BOTH:

  A) Multi-pro matches: >= N known pros in the same match.
  B) Single-pro standout performances: a Recognised Pro with a notable
     individual game (K/D, ADR, or kills above threshold).

Stats are fetched once per match (deduped cache). No per-player ELO lookups,
so it finishes quickly instead of timing out like `faceit recent`.

Usage:
    python scripts/faceit/scrape_notable.py [--hours 48] [--count 25]
        [--min-pros 2] [--top 20] [--limit 5]
        [--perf-kd 1.5] [--perf-adr 100] [--perf-kills 30] [--perf-limit 120]
        [--exclude-today] [--today-only]

    --limit caps how many matches are printed from EACH stream (multi-pro
    and single-pro standout). Defaults to 5 per stream.
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


async def collect(*, hours: int, count: int, min_pros: int,
                  perf_kd: float, perf_adr: float, perf_kills: int,
                  perf_limit: int, today_only: bool = False,
                  exclude_today: bool = False) -> dict:
    """Scrape notable FACEIT matches. Returns a dict for reporting / reuse:

      {
        "hours": int, "today_only": bool,
        "multi": [ {id, pros:[nick], map, score, date, team1, team2,
                     avg_elo, elo_n, elo_tot, players:{nick: line}} ... ],
        "solo":  [ {id, pro, line, map, score, date, team1, team2,
                     avg_elo, elo_n, elo_tot} ... ],
        "multi_total": int, "solo_total": int, "solo_evaluated": int,
        "solo_available": int,
      }

    ``multi`` is pre-sorted (most pros, then most recent); ``solo`` is
    pre-sorted (best K/D, then ADR). ELO averages are filled for entries
    (cached per player internally).
    """
    pros = pro_nicks()
    if not pros:
        raise RuntimeError("No Recognised Pros in .data/player_accounts.json")

    client = FACEITClient()
    try:
        match_pros: dict[str, set] = {}
        match_meta: dict[str, dict] = {}
        cutoff = datetime.now() - timedelta(hours=hours)
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if today_only:
            cutoff = today_start

        for nick in pros:
            q = faceit_nick(nick)
            pid = await client.get_player_id(q)
            if not pid:
                continue
            matches = await client.get_player_matches(pid, limit=count)
            for m in matches:
                if m.date and m.date < cutoff:
                    continue
                if exclude_today and today_start is not None \
                        and m.date and m.date >= today_start:
                    continue
                match_pros.setdefault(m.match_id, set()).add(nick)
                match_meta.setdefault(m.match_id, {
                    "id": m.match_id, "date": m.date,
                    "team1": m.team1, "team2": m.team2,
                })

        multi = {mid: ps for mid, ps in match_pros.items() if len(ps) >= min_pros}
        single = {mid: ps for mid, ps in match_pros.items()
                  if len(ps) == 1 and mid not in multi}

        stats_cache: dict[str, dict] = {}

        async def get_stats(mid: str):
            if mid not in stats_cache:
                stats_cache[mid] = await client.get_match_stats(mid) or {}
            return stats_cache[mid]

        # ---- solo standout performances ----
        solo = []
        single_ids = list(single.keys())[: perf_limit]
        for mid in single_ids:
            stats = await get_stats(mid)
            if not stats:
                continue
            pro = next(iter(single[mid]))
            line = stats.get("players", {}).get(pro)
            if not line:
                continue
            if _is_notable_perf(line, perf_kd, perf_adr, perf_kills):
                solo.append({
                    "id": mid, "pro": pro, "line": line,
                    "map": stats.get("map", "?"),
                    "score": stats.get("score", ""),
                    "date": match_meta.get(mid, {}).get("date"),
                    "team1": match_meta.get(mid, {}).get("team1", "?"),
                    "team2": match_meta.get(mid, {}).get("team2", "?"),
                })
        solo.sort(key=lambda r: (-_num(r['line'].get('kd'), float),
                                 -_num(r['line'].get('adr'), float)))

        # ---- multi-pro matches (ranked: most pros, then recent) ----
        ranked_multi = sorted(
            multi.items(),
            key=lambda x: (-len(x[1]),
                           -(match_meta.get(x[0], {}).get("date") or datetime.min).timestamp()),
        )

        # pre-fetch stats for all ranked multi + all solo (for ELO averages)
        for mid, _ in ranked_multi:
            await get_stats(mid)
        for s in solo:
            await get_stats(s["id"])

        # average lobby ELO per match (cached per player)
        _elo_sem = asyncio.Semaphore(8)
        _elo_cache: dict[str, tuple] = {}

        async def lobby_elos(mid: str) -> tuple:
            if mid in _elo_cache:
                return _elo_cache[mid]
            stats = stats_cache.get(mid, {})
            pids = [p.get("player_id") for p in stats.get("players", {}).values()
                    if p.get("player_id")]
            async with _elo_sem:
                els = await asyncio.gather(*[client.get_player_elo(pid) for pid in pids])
            vals = [e for e in els if e is not None]
            res = (round(sum(vals) / len(vals)) if vals else None, len(vals), len(pids))
            _elo_cache[mid] = res
            return res

        multi_out = []
        for mid, ps in ranked_multi:
            stats = stats_cache.get(mid, {})
            meta = match_meta.get(mid, {})
            players = {nick: stats.get("players", {}).get(nick)
                       for nick in ps if stats.get("players", {}).get(nick)}
            avg, n, tot = await lobby_elos(mid)
            multi_out.append({
                "id": mid, "pros": sorted(ps), "players": players,
                "map": stats.get("map", "?"), "score": stats.get("score", ""),
                "date": meta.get("date"), "team1": meta.get("team1", "?"),
                "team2": meta.get("team2", "?"),
                "avg_elo": avg, "elo_n": n, "elo_tot": tot,
            })

        for s in solo:
            avg, n, tot = await lobby_elos(s["id"])
            s["avg_elo"] = avg
            s["elo_n"] = n
            s["elo_tot"] = tot

        return {
            "hours": hours,
            "today_only": today_only,
            "multi_total": len(multi),
            "solo_total": len(solo),
            "solo_evaluated": len(single_ids),
            "solo_available": len(single),
            "multi": multi_out,
            "solo": solo,
        }
    finally:
        await client.close()
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--count", type=int, default=25)
    ap.add_argument("--min-pros", type=int, default=2)
    ap.add_argument("--top", type=int, default=20,
                    help="Detailed stats for the N most-notable multi-pro matches "
                         "(most pros, then most recent).")  # prefetch/eval cap
    ap.add_argument("--limit", type=int, default=5,
                    help="Cap for matches printed from EACH stream "
                         "(multi-pro and single-pro; default 5 per stream).")
    ap.add_argument("--today-only", action="store_true",
                    help="Restrict the window to today only (start of local day).")
    ap.add_argument("--exclude-today", action="store_true",
                    help="Exclude matches from today (keep --hours window).")
    ap.add_argument("--perf-kd", type=float, default=1.5,
                    help="Single-pro K/D threshold for a notable performance.")
    ap.add_argument("--perf-adr", type=float, default=100.0,
                    help="Single-pro ADR threshold for a notable performance.")
    ap.add_argument("--perf-kills", type=int, default=30,
                    help="Single-pro kills threshold for a notable performance.")
    ap.add_argument("--perf-limit", type=int, default=120,
                    help="Max number of single-pro matches to evaluate for performance.")
    args = ap.parse_args()

    try:
        data = await collect(
            hours=args.hours, count=args.count, min_pros=args.min_pros,
            perf_kd=args.perf_kd, perf_adr=args.perf_adr,
            perf_kills=args.perf_kills, perf_limit=args.perf_limit,
            today_only=args.today_only, exclude_today=args.exclude_today,
        )
    except RuntimeError as e:
        print(f"[ERR] {e}")
        return

    _limit = max(args.limit, 1)
    window_label = "today" if data["today_only"] else f"last {data['hours']}h"
    print(f"\n=== FACEIT NOTABLE MATCHES — {window_label} "
          f"(multi-pro + single standout) ===")

    print(f"\n--- A) MULTI-PRO MATCHES (>= {args.min_pros} pros): {data['multi_total']} found "
          f"(showing top {min(_limit, data['multi_total'])}) ---\n")
    for rec in data["multi"][: _limit]:
        datestr = rec["date"].strftime("%Y-%m-%d") if rec["date"] else "?"
        print(f"* {rec['id']}  ({len(rec['pros'])} pros)  {rec['map']}  "
              f"{rec['score']}  {datestr}")
        print(f"    teams: {rec['team1']} vs {rec['team2']}")
        print(f"    pros:  {', '.join(rec['pros'])}")
        for nick, line in rec["players"].items():
            print(f"      - {nick}: {line.get('kills')}/{line.get('deaths')} "
                  f"(K/D {line.get('kd')}, ADR {line.get('adr')}, HS% {line.get('hs')})")
        if rec["avg_elo"] is not None:
            print(f"    avg lobby ELO: {rec['avg_elo']} ({rec['elo_n']}/{rec['elo_tot']} players)")
        else:
            print("    avg lobby ELO: n/a")
        print()

    print(f"--- B) SINGLE-PRO STANDOUT PERFORMANCES "
          f"(K/D>={args.perf_kd} or ADR>={args.perf_adr} or kills>={args.perf_kills}): "
          f"{data['solo_total']} found "
          f"(evaluated {data['solo_evaluated']}/{data['solo_available']} "
          f"single-pro matches) ---\n")
    for rec in data["solo"][: _limit]:
        line = rec["line"]
        datestr = rec["date"].strftime("%Y-%m-%d") if rec["date"] else "?"
        print(f"* {rec['id']}  {rec['map']}  {rec['score']}  {datestr}")
        print(f"    teams: {rec['team1']} vs {rec['team2']}")
        print(f"    {rec['pro']}: {line.get('kills')}/{line.get('deaths')} "
              f"(K/D {line.get('kd')}, ADR {line.get('adr')}, HS% {line.get('hs')})")
        if rec["avg_elo"] is not None:
            print(f"    avg lobby ELO: {rec['avg_elo']} ({rec['elo_n']}/{rec['elo_tot']} players)")
        else:
            print("    avg lobby ELO: n/a")
        print()


if __name__ == "__main__":
    asyncio.run(main())
