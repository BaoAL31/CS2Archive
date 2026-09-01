"""
FACEIT notable-match scrape.

Discovers multi-pro lobbies and single-pro standout lines, then scores every
Recognised-Pro performance with the market-demand weight system (YouTube
player demand, lobby ELO, HLTV rank / 4, trio+ co-stars, bounded match
perf). `collect()` returns those scored candidates; `daily_notable.py`
(daily FACEIT notable) only picks from them. The HLTV match listener runs
that picker on days with no tournament matches.

Usage:
    python scripts/faceit/scrape_notable.py [--hours 48] [--count 25]
        [--min-pros 2] [--top 20] [--limit 5]
        [--perf-kd 1.5] [--perf-adr 100] [--perf-kills 30] [--perf-limit 120]
        [--exclude-today] [--today-only]

    --limit caps how many scored player POVs are printed (default 5).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure
ensure()

from player_accounts import list_accounts  # noqa: E402
from scrapers.faceit import FACEITClient  # noqa: E402
from faceit_names import known_pro_faceit_ids  # noqa: E402
from hltv_ranking import fetch_team_ranking, rank_bonus, star_bonus_for_pros  # noqa: E402
from scoring import (  # noqa: E402
    PLAYER_DEMAND_INDEX,
    costar_bonus,
    lobby_elo_bonus,
    perf_bonus as _perf_bonus,
    star_bonus,
)
import scoring as _scoring  # noqa: E402

DEMAND_INDEX_PATH = _scoring.DEMAND_INDEX_PATH


def pro_nicks() -> list[str]:
    return [a.nickname for a in list_accounts() if a.nickname]


def _warn_unverified_accounts(fid_to_nick: dict) -> None:
    """Log any Recognised Pro account lacking a stored faceit_id.

    Those cannot be identity-verified (nickname queries are spoofable) and are
    therefore skipped by collect().
    """
    known = set(fid_to_nick.values())
    missing = [a.nickname for a in list_accounts()
               if a.nickname and a.nickname not in known]
    if missing:
        print(f"[warn] {len(missing)} pro(s) skipped — no stored faceit_id "
              f"(identity unverifiable): {', '.join(sorted(missing))}")


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


def is_good_faceit_pov(c: dict) -> bool:
    """Plus-K/D win from an HLTV top-10 org (donk / kyousuke / m0NESY tier).

    High K/D/ADR is not a substitute — that is usually a stomped low-ELO
    lobby (blameF 27/9 vs ~2500). ``raw_star_bonus`` is ``rank_bonus`` for
    the POV's org (250k at rank 10).
    """
    if not c.get("won"):
        return False
    if _num(c.get("kd"), float) < 1.0:
        return False
    return _num(c.get("raw_star_bonus"), int) >= rank_bonus(10)


def load_player_demand_index():
    return _scoring.load_player_demand_index(DEMAND_INDEX_PATH)


def market_demand_bonus(nick: str) -> int:
    return _scoring.market_demand_bonus(nick, DEMAND_INDEX_PATH)


SCORE_VERSION = 5


def _line_won(line: dict) -> bool:
    """Interpret a FACEIT result field as a win."""
    r = line.get("result")
    if r is None:
        return False
    if isinstance(r, str):
        return r.strip().lower() in ("1", "1.0", "true")
    return bool(r)


def rescore_stored(c: dict) -> dict:
    """Rebuild current bonuses from a persisted candidate's stats."""
    out = dict(c)
    won = bool(c.get("won"))
    kd = _num(c.get("kd"), float)
    adr = _num(c.get("adr"), float)
    kills = _num(c.get("kills"), int)
    raw = _num(c.get("raw_star_bonus"), int)
    if raw <= 0:
        stored_star = _num(c.get("star_bonus"), int)
        raw = stored_star * 4 if stored_star in (100_000, 62_500, 30_000, 15_000) else stored_star
    nick = c.get("player") or ""
    star = star_bonus(raw, won, kd)
    demand = market_demand_bonus(nick)
    elo = lobby_elo_bonus(c.get("avg_elo") or 0)
    costars = costar_bonus(c.get("pros") or []) if won else 0
    perf = _perf_bonus(kd, adr, kills, won)
    out.update({
        "raw_star_bonus": raw,
        "star_bonus": star,
        "market_demand_bonus": demand,
        "lobby_elo_bonus": elo,
        "costar_bonus": costars,
        "perf_bonus": perf,
        "score_version": SCORE_VERSION,
        "weight": star + demand + elo + costars + perf,
    })
    if not out.get("id") and out.get("match_id") and nick:
        out["id"] = f"{out['match_id']}:{nick}"
    return out


def make_player_candidates(rec: dict, stream: str, ranking: dict | None = None) -> list[dict]:
    """Expand a scrape record into one candidate per Recognised Pro performance.

    Unit of selection is a PLAYER performance (not a match). Every score
    component is retained on the candidate so daily picks remain explainable.
    """
    date = rec.get("date")
    datestr = date.strftime("%Y-%m-%d") if date else None
    out = []

    def _build(nick: str, line: dict, match_pros: list[str]) -> dict:
        kd = _num(line.get("kd"), float)
        adr = _num(line.get("adr"), float)
        kills = _num(line.get("kills"), int)
        won = _line_won(line)
        raw_star = star_bonus_for_pros([nick], ranking)
        star = star_bonus(raw_star, won, kd)
        demand = market_demand_bonus(nick)
        elo = lobby_elo_bonus(_num(rec.get("avg_elo"), float))
        costars = costar_bonus(match_pros) if won else 0
        perf = _perf_bonus(kd, adr, kills, won)
        return {
            "id": f"{rec['id']}:{nick}",
            "match_id": rec["id"],
            "player": nick,
            "stream": stream,
            "map": rec.get("map", "?"),
            "score": rec.get("score", ""),
            "date": datestr,
            "pros": match_pros,
            "kd": round(kd, 2),
            "adr": adr,
            "hs": _num(line.get("hs"), float),
            "kills": kills,
            "deaths": _num(line.get("deaths"), int),
            "won": won,
            "avg_elo": rec.get("avg_elo"),
            "score_version": SCORE_VERSION,
            "raw_star_bonus": raw_star,
            "star_bonus": star,
            "market_demand_bonus": demand,
            "lobby_elo_bonus": elo,
            "costar_bonus": costars,
            "perf_bonus": perf,
            "weight": star + demand + elo + costars + perf,
        }

    if stream == "multi":
        match_pros = rec.get("pros", [])
        for nick, line in rec.get("players", {}).items():
            if not line:
                continue
            out.append(_build(nick, line, match_pros))
    else:
        line = rec.get("line", {})
        nick = rec.get("pro") or "?"
        out.append(_build(nick, line, [nick]))
    return out


def score_candidates(multi: list[dict], solo: list[dict], ranking: dict | None) -> list[dict]:
    """Score every Recognised-Pro line and rank by weight, then recency."""
    out: list[dict] = []
    for rec in multi:
        out.extend(make_player_candidates(rec, "multi", ranking))
    for rec in solo:
        out.extend(make_player_candidates(rec, "solo", ranking))
    out.sort(key=lambda c: (
        -c["weight"],
        -(datetime.strptime(c["date"], "%Y-%m-%d").timestamp() if c.get("date") else 0),
    ))
    return out


async def collect(*, hours: int, count: int, min_pros: int,
                  perf_kd: float, perf_adr: float, perf_kills: int,
                  perf_limit: int, today_only: bool = False,
                  exclude_today: bool = False,
                  as_of: datetime | None = None) -> dict:
    """Scrape notable FACEIT matches and score every Recognised-Pro line.

    Returns a dict for reporting / reuse:

      {
        "hours": int, "today_only": bool,
        "multi": [ {id, pros:[nick], map, score, date, team1, team2,
                     avg_elo, elo_n, elo_tot, players:{nick: line}} ... ],
        "solo":  [ {id, pro, line, map, score, date, team1, team2,
                     avg_elo, elo_n, elo_tot} ... ],
        "candidates": [ scored player-POV dicts, weight desc ],
        "multi_total": int, "solo_total": int, "solo_evaluated": int,
        "solo_available": int,
      }

    ``candidates`` is the ranked unit daily FACEIT notable picks from. ``multi`` /
    ``solo`` stay as match-level source records (ELO filled).
    """
    fid_to_nick = known_pro_faceit_ids()
    if not fid_to_nick:
        raise RuntimeError(
            "No Recognised Pros with a stored faceit_id in .data/player_accounts.json"
        )
    _warn_unverified_accounts(fid_to_nick)

    client = FACEITClient()
    try:
        match_pros: dict[str, set] = {}
        match_meta: dict[str, dict] = {}
        now = as_of or datetime.now()
        cutoff = now - timedelta(hours=hours)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if today_only:
            cutoff = today_start
        horizon = now if as_of is not None else None

        for fid, nick in fid_to_nick.items():
            # NEVER resolve identity by nickname: FACEIT allows duplicate nicks,
            # so get_player_id("donk") can return an impostor account. The stored
            # faceit_id (curated in player_accounts.json) is the only trustworthy
            # key — fetch the account's matches by that GUID directly.
            matches = await client.get_player_matches(fid, limit=count)
            for m in matches:
                if m.date and m.date < cutoff:
                    continue
                if horizon is not None and m.date and m.date > horizon:
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

        ranking = await fetch_team_ranking()
        candidates = score_candidates(multi_out, solo, ranking)

        return {
            "hours": hours,
            "today_only": today_only,
            "multi_total": len(multi),
            "solo_total": len(solo),
            "solo_evaluated": len(single_ids),
            "solo_available": len(single),
            "multi": multi_out,
            "solo": solo,
            "candidates": candidates,
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
                    help="Cap for scored player POVs printed (weight desc; default 5).")
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
    cands = data["candidates"][:_limit]
    print(f"\n=== FACEIT NOTABLE PERFORMANCES — {window_label} ===")
    print(
        f"{data['multi_total']} multi-pro matches, {data['solo_total']} solo standouts "
        f"({data['solo_evaluated']}/{data['solo_available']} single-pro matches evaluated) "
        f"→ {len(data['candidates'])} player POVs "
        f"(showing top {len(cands)} by weight)\n"
    )
    for rec in cands:
        costars = [p for p in rec.get("pros", []) if p != rec["player"]]
        with_bit = f"  (w/ {', '.join(costars)})" if costars else ""
        result = "won" if rec.get("won") else "lost"
        print(f"* {rec['player']}  [{rec['match_id']}]  ({rec['stream']})")
        print(f"    {rec['map']}  {rec['score']}  {rec.get('date') or '?'}{with_bit}")
        print(
            f"    {rec.get('kills')}/{rec.get('deaths')} "
            f"(K/D {rec.get('kd')}, ADR {rec.get('adr')}) - {result} | "
            f"ELO {rec.get('avg_elo') or '?'}"
        )
        print(
            f"    team={rec.get('star_bonus')} demand={rec.get('market_demand_bonus')} "
            f"elo={rec.get('lobby_elo_bonus')} costars={rec.get('costar_bonus')} "
            f"perf={rec.get('perf_bonus')} total={rec.get('weight')}"
        )
        print()


if __name__ == "__main__":
    asyncio.run(main())
