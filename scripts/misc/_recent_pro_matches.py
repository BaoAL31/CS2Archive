import asyncio, json, sys, time
from datetime import datetime, timedelta

sys.path.insert(0, "scripts")
from _pathsetup import ensure
ensure()
from scrapers.faceit import FACEITClient

HOURS = 48  # last 2 days


def load_accounts():
    return json.load(open(".data/player_accounts.json", encoding="utf-8"))


async def main():
    accounts = load_accounts()
    pros = {}
    for p in accounts:
        pid = (p or {}).get("faceit_id")
        if pid:
            pros[p["nickname"]] = pid
    print(f"tracking {len(pros)} pros with faceit_id", flush=True)

    cutoff = time.time() - HOURS * 3600
    fc = FACEITClient()

    # Step 1: aggregate recent match_ids from every pro's history
    match_pros = {}  # match_id -> set of pro nicknames (seen in a pro's own history)
    match_meta = {}  # match_id -> (date, team1, team2)
    for nick, pid in pros.items():
        try:
            matches = await fc.get_player_matches(pid, limit=30)
        except Exception as e:
            print(f"  [warn] {nick}: history failed {e}", flush=True)
            continue
        for m in matches:
            if not m.date:
                continue
            if m.date.timestamp() < cutoff:
                continue
            match_pros.setdefault(m.match_id, set()).add(nick)
            match_meta.setdefault(m.match_id, (m.date, m.team1, m.team2))
        await asyncio.sleep(0.05)  # gentle rate limit
    await fc.close()

    candidates = {mid for mid, ps in match_pros.items() if len(ps) >= 1}
    print(f"{len(candidates)} unique matches in last {HOURS}h from pro histories", flush=True)

    # Step 2: fetch stats for each candidate to count full pro roster (rate-limited + retry)
    fc = FACEITClient()
    pro_id_to_nick = {v: k for k, v in pros.items()}
    results = []
    ordered = sorted(candidates, key=lambda m: match_meta[m][0].timestamp(), reverse=True)
    for mid in ordered:
        st = None
        for attempt in range(4):
            st = await _stats_guard(fc, mid)
            if st is not None:
                break
            await asyncio.sleep(1.5 * (attempt + 1))  # backoff on 429
        if st is None:
            print(f"  [skip] {mid} stats failed after retries", flush=True)
            continue
        roster = set()
        for nick, p in st.get("players", {}).items():
            pid = p.get("player_id")
            if pid and pid in pro_id_to_nick:
                roster.add(pro_id_to_nick[pid])
        date, t1, t2 = match_meta[mid]
        results.append({
            "match_id": mid,
            "date": date.strftime("%Y-%m-%d %H:%M"),
            "map": st.get("map"),
            "score": st.get("score"),
            "teams": st.get("teams"),
            "pros": sorted(roster),
            "pro_count": len(roster),
            "seen_by_pros": sorted(match_pros[mid]),
            "url": f"https://www.faceit.com/en/cs2/room/{mid}",
        })
        await asyncio.sleep(1.1)  # stay under the rate limit
    await fc.close()

    multi = [r for r in results if r["pro_count"] >= 2]
    print("\n=== MATCHES WITH >=2 TRACKED PROS (last 2 days) ===")
    for r in sorted(multi, key=lambda x: x["date"], reverse=True):
        print(f"\n[{r['date']}] {r['map']}  score={r['score']}  teams={r['teams']}")
        print(f"  PROS({r['pro_count']}): {', '.join(r['pros'])}")
        print(f"  url: {r['url']}")
    print(f"\nTotal multi-pro matches: {len(multi)}")
    print(f"Single/other matches checked: {len(results)}")


async def _stats_guard(fc, mid):
    try:
        return await fc.get_match_stats(mid)
    except Exception as e:
        return None


if __name__ == "__main__":
    asyncio.run(main())
