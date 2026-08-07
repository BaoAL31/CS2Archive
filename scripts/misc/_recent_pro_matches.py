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
    pros = {}   # nickname -> faceit_id
    pro_steam = {}
    for p in accounts:
        pid = (p or {}).get("faceit_id")
        if pid:
            pros[p["nickname"]] = pid
            pro_steam[p["nickname"]] = p.get("steam_id")
    print(f"tracking {len(pros)} pros with faceit_id", flush=True)

    cutoff = time.time() - HOURS * 3600
    fc = FACEITClient()

    # Step 1: aggregate recent match_ids from every pro's history
    match_pros = {}  # match_id -> set of pro nicknames
    match_meta = {}  # match_id -> (date, team1, team2)
    for nick, pid in pros.items():
        try:
            matches = await fc.get_player_matches(pid, limit=30)
        except Exception as e:
            print(f"  [warn] {nick}: history failed {e}", flush=True)
            continue
        for m in matches:
            if not m.date or m.date.timestamp() < cutoff:
                continue
            match_pros.setdefault(m.match_id, set()).add(nick)
            match_meta.setdefault(m.match_id, (m.date, m.team1, m.team2))
        await asyncio.sleep(0.05)
    await fc.close()

    candidates = {mid for mid, ps in match_pros.items() if len(ps) >= 1}
    print(f"{len(candidates)} unique matches in last {HOURS}h from pro histories", flush=True)

    # Step 2: fetch stats + per-pro ELO/K/D for each candidate (rate-limited + retry)
    fc = FACEITClient()
    pro_id_to_nick = {v: k for k, v in pros.items()}
    elo_cache = {}

    async def elo_for(nick):
        if nick in elo_cache:
            return elo_cache[nick]
        pid = pros.get(nick)
        val = None
        if pid:
            for a in range(3):
                try:
                    val = await fc.get_player_elo(pid)
                    break
                except Exception:
                    await asyncio.sleep(1.2 * (a + 1))
        elo_cache[nick] = val
        return val

    steam_cache = {}

    async def steam_for_faceit(pid):
        """Steam id for a FACEIT player id, cached. None on failure."""
        if pid in steam_cache:
            return steam_cache[pid]
        val = None
        for a in range(3):
            try:
                val = await fc.get_player_steam_id(pid)
                break
            except Exception:
                await asyncio.sleep(1.2 * (a + 1))
        steam_cache[pid] = val
        return val

    results = []
    ordered = sorted(candidates, key=lambda m: match_meta[m][0].timestamp(), reverse=True)
    for mid in ordered:
        st = None
        for attempt in range(4):
            st = await _stats_guard(fc, mid)
            if st is not None:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
        if st is None:
            print(f"  [skip] {mid} stats failed after retries", flush=True)
            continue
        roster = set()
        players = st.get("players", {})
        for nick, p in players.items():
            pid = p.get("player_id")
            if not (pid and pid in pro_id_to_nick):
                continue
            acc_nick = pro_id_to_nick[pid]
            sid = await steam_for_faceit(pid)
            if sid is not None and str(sid) == str(pro_steam.get(acc_nick)):
                roster.add(acc_nick)
            else:
                print(f"  [warn] {acc_nick}: faceit {pid} steam {sid} "
                      f"!= account steam {pro_steam.get(acc_nick)}; skipping", flush=True)
        # per-pro elo + kd — look up stats by FACEIT id (the in-game nickname in
        # the stats can differ from the account nickname, e.g. donk -> donk666).
        pid_to_stats = {p.get("player_id"): p for p in players.values() if p.get("player_id")}
        pro_detail = []
        for nick in sorted(roster):
            pid = pros.get(nick)
            info = pid_to_stats.get(pid, {}) if pid else {}
            kd = info.get("kd")
            if kd in (None, "?", "-1", "-"):
                kd = None
            try:
                kd = float(kd) if kd is not None else None
            except (TypeError, ValueError):
                kd = None
            elo = await elo_for(nick)
            pro_detail.append({"nick": nick, "elo": elo, "kd": kd})
            await asyncio.sleep(0.15)
        date, t1, t2 = match_meta[mid]
        results.append({
            "match_id": mid,
            "date": date.strftime("%Y-%m-%d %H:%M"),
            "map": st.get("map"),
            "score": st.get("score"),
            "teams": st.get("teams"),
            "pros": sorted(roster),
            "pro_count": len(roster),
            "pro_detail": pro_detail,
            "max_elo": max((d["elo"] or 0 for d in pro_detail), default=0),
            "max_kd": max((d["kd"] or 0 for d in pro_detail), default=0),
            "url": f"https://www.faceit.com/en/cs2/room/{mid}",
        })
        await asyncio.sleep(1.1)
    await fc.close()

    multi = [r for r in results if r["pro_count"] >= 2]
    single = [r for r in results if r["pro_count"] == 1]

    print("\n" + "=" * 60)
    print("MULTI-PRO MATCHES (>=2 pros), ranked by # pros then recency")
    print("=" * 60)
    multi.sort(key=lambda r: -r["pro_count"])
    for r in multi:
        detail = ", ".join(f"{d['nick']}(elo {d['elo']}, kd {d['kd']})" for d in r["pro_detail"])
        print(f"\n[{r['date']}] {r['map']}  score={r['score']}")
        print(f"  PROS({r['pro_count']}): {', '.join(r['pros'])}")
        print(f"  {detail}")
        print(f"  {r['url']}")
    print(f"\nTotal multi-pro matches: {len(multi)}")

    print("\n" + "=" * 60)
    print("SINGLE-PRO MATCHES, ranked by ELO (then K/D)")
    print("=" * 60)
    single.sort(key=lambda r: (-r["max_elo"], -r["max_kd"]))
    for r in single:
        d = r["pro_detail"][0]
        print(f"[{r['date']}] {r['map']}  score={r['score']}  "
              f"{d['nick']}(elo {d['elo']}, kd {d['kd']})  {r['url']}")
    print(f"\nTotal single-pro matches: {len(single)}")


async def _stats_guard(fc, mid):
    try:
        return await fc.get_match_stats(mid)
    except Exception:
        return None


if __name__ == "__main__":
    asyncio.run(main())
