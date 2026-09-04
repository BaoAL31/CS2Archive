"""
Daily FACEIT notable-match selector.

Picks the top N (=3) Recognised-Pro POVs for a calendar day from
`scrape_notable.collect()`. If the day's scrape cannot fill N, falls back
to performances left in the persistent pool from previous days.

The HLTV match listener polls this on off days (no tournament match live
or starting within 12 hours) and queues only watchable POVs — it does not
pad the day to 3. Manual CLI still works.

``pick_for_day`` is idempotent per calendar day. The listener uses
``discover_good_povs`` instead, which re-scrapes and only returns heaters
not already in ``used``.

State file: .data/notable_daily.json
  {
    "last_day": "YYYY-MM-DD",
    "picks": { "<day>": [ <pick>, ... ] },
    "used":  [ "<match_id>", ... ],
    "pool":  [ <candidate>, ... ],   # notable, not yet picked
  }

Usage:
    python scripts/faceit/daily_notable.py            # discover + pick + persist
    python scripts/faceit/daily_notable.py --dry-run  # report without persisting
    python scripts/faceit/daily_notable.py --json     # machine-readable picks
    python scripts/faceit/daily_notable.py --download # also download demos + build backlog
    python scripts/faceit/daily_notable.py --replay-from 2026-08-25 --replay-to 2026-08-29 --from-state
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from scrape_notable import (  # noqa: E402
    SCORE_VERSION,
    collect,
    is_good_faceit_pov,
    rescore_stored,
)
from update_player_demand import refresh as refresh_player_demand

STATE_FILE = ROOT / ".data" / "notable_daily.json"
DEMO_DIR = ROOT / "demos" / "faceit"
PY = sys.executable
DEFAULT_PICKS = 3
DEFAULT_HOURS = 24


# ---------- selection ----------
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_day": None, "picks": {}, "used": [], "pool": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _reason(c: dict) -> str:
    w = "won" if c.get("won") else "lost"
    return f"{c['player']} {c.get('kills')}/{c.get('deaths')} (K/D {c.get('kd')}, ADR {c.get('adr')}) - {w}"


def _cand_day(c: dict) -> str | None:
    raw = c.get("date")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    return str(raw)[:10]


def day_already_picked(state: dict, today: str) -> bool:
    """True when this calendar day already has a persisted pick run."""
    if state.get("last_day") != today:
        return False
    if today not in (state.get("picks") or {}):
        return False
    prev = state["picks"][today]
    return all(c.get("score_version") == SCORE_VERSION for c in prev)


def replay_days(
    candidates: list[dict], start: str, end: str, n: int,
    window_hours: int = DEFAULT_HOURS,
) -> dict[str, list[dict]]:
    """Walk calendar days as the daily FACEIT notable picker would: 24h lookback, one POV per
    player and match, already-picked performance ids stay used."""
    start_d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    used: list[str] = []
    out: dict[str, list[dict]] = {}
    day = start_d
    while day <= end_d:
        ds = day.strftime("%Y-%m-%d")
        window_start = (day - timedelta(hours=window_hours)).strftime("%Y-%m-%d")
        window = []
        for c in candidates:
            if not c.get("id"):
                continue
            cd = _cand_day(c)
            if not cd or cd < window_start or cd > ds:
                continue
            window.append(c)
        picks, new_used, _ = select({"used": used}, window, n, ds)
        used.extend(new_used)
        out[ds] = picks
        day += timedelta(days=1)
    return out


def select(state: dict, candidates: list[dict], n: int, today: str) -> tuple[list[dict], list[str], list[dict]]:
    """Pick up to n best player-performances, one per distinct player.

    Skips already-used performance ids (persisted across days). Returns
    (picks, newly_used_ids, survivors). Candidates include fresh scrape +
    the persistent pool from previous days (fallback).
    """
    used = set(state.get("used", []))
    fresh = [c for c in candidates if c["id"] not in used]
    fresh.sort(key=lambda c: (-c["weight"],
                              -(datetime.strptime(c["date"], "%Y-%m-%d").timestamp()
                                if c.get("date") else 0)))
    picks, taken_players, taken_matches = [], set(), set()
    for c in fresh:
        if len(picks) >= n:
            break
        if c["player"] in taken_players:   # one pick per player
            continue
        if c["match_id"] in taken_matches:  # one POV/backlog action per match
            continue
        taken_players.add(c["player"])
        taken_matches.add(c["match_id"])
        picks.append(c)
    picked_ids = {c["id"] for c in picks}
    new_used = list(picked_ids)
    # survivors stay in the pool for future days
    survivors = [c for c in candidates
                 if c["id"] not in picked_ids and c["id"] not in used]
    return picks, new_used, survivors


def choose_picks(
    state: dict, fresh: list[dict], n: int, today: str,
) -> tuple[list[dict], list[str]]:
    """Take up to n watchable POVs from this scrape. Do not pad with leftovers."""
    good = [c for c in fresh if is_good_faceit_pov(c)]
    picks, new_used, _ = select(state, good, n, today)
    return picks, new_used


def remember_picks(picks: list[dict]) -> None:
    """Record queued POVs so later scrapes do not repeat them."""
    if not picks:
        return
    state = _load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    used = state.setdefault("used", [])
    for pick in picks:
        pid = pick.get("id")
        if pid and pid not in used:
            used.append(pid)
    existing = list(state.setdefault("picks", {}).get(today) or [])
    have = {c.get("id") for c in existing}
    for pick in picks:
        if pick.get("id") and pick["id"] not in have:
            existing.append(pick)
    state["picks"][today] = existing
    state["last_day"] = today
    _save_state(state)


async def discover_good_povs(
    n: int = DEFAULT_PICKS,
    *,
    hours: int = DEFAULT_HOURS,
    count: int = 25,
    skip_youtube_demand: bool = True,
) -> list[dict]:
    """Scrape the lookback window and return unused watchable POVs (up to n)."""
    state = _load_state()
    if not skip_youtube_demand:
        try:
            demand = refresh_player_demand(scrape=True)
            print(
                f"[DEMAND] {len(demand['index'])} players from "
                f"{demand['window_videos']} videos "
                f"(scraped {demand['scraped']} new/updated)"
            )
        except Exception as exc:
            print(f"[DEMAND] skipped ({exc})")
    data = await collect(
        hours=hours, count=count, min_pros=2,
        perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
        today_only=False, exclude_today=False,
    )
    today = datetime.now().strftime("%Y-%m-%d")
    picks, _, _ = select(
        {"used": state.get("used") or []},
        [c for c in data["candidates"] if is_good_faceit_pov(c)],
        n,
        today,
    )
    return picks


def _format_pick(c: dict) -> str:
    when = c.get("date") or "?"
    return (
        f"  * {c['player']}  [{c['match_id']}]\n"
        f"      {c['map']} {c['score']} {c.get('date') or '?'} "
        f"{' (w/ ' + ', '.join(p for p in c.get('pros', []) if p != c['player']) + ')' if any(p != c['player'] for p in c.get('pros', [])) else ''}\n"
        f"      {_reason(c)} | ELO {c.get('avg_elo') or '?'} | "
        f"team={c.get('star_bonus')} demand={c.get('market_demand_bonus')} "
        f"elo={c.get('lobby_elo_bonus')} costars={c.get('costar_bonus')} "
        f"perf={c.get('perf_bonus')} total={c.get('weight')}"
    )


def card_for_pick(pick: dict, *, root: Path = ROOT) -> Path | None:
    """Backlog card written for this pick's player + FACEIT match id."""
    match_id = str(pick.get("match_id") or "")
    player = str(pick.get("player") or "").casefold()
    if not match_id or not player:
        return None
    for faceit in (root / "faceit", root / "backlog" / "faceit"):
        if not faceit.is_dir():
            continue
        for path in faceit.rglob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(meta.get("faceit_match_id") or "") != match_id:
                continue
            nicks = {
                str(meta.get("player") or "").casefold(),
                str(meta.get("faceit_nickname") or "").casefold(),
            }
            if player in nicks:
                return path
    return None


def rel_card_for_pick(pick: dict, *, root: Path = ROOT) -> str | None:
    path = card_for_pick(pick, root=root)
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


async def pick_for_day(
    n: int = DEFAULT_PICKS,
    *,
    today: str | None = None,
    hours: int = DEFAULT_HOURS,
    count: int = 25,
    force: bool = False,
    dry_run: bool = False,
    skip_youtube_demand: bool = False,
) -> list[dict]:
    """Discover and persist today's FACEIT notable picks. Idempotent per day."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    state = _load_state()

    if not force and day_already_picked(state, today):
        return list(state["picks"].get(today) or [])

    if not skip_youtube_demand:
        try:
            demand = refresh_player_demand(scrape=True)
            print(
                f"[DEMAND] {len(demand['index'])} players from "
                f"{demand['window_videos']} videos "
                f"(scraped {demand['scraped']} new/updated)"
            )
        except Exception as exc:
            print(f"[DEMAND] skipped ({exc})")

    data = await collect(
        hours=hours, count=count, min_pros=2,
        perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
        today_only=False, exclude_today=False,
    )
    fresh = data["candidates"]
    old_pool = [
        c for c in state["pool"]
        if c.get("id") and c.get("score_version") == SCORE_VERSION
    ]
    picks, new_used = choose_picks(
        {"used": state.get("used") or [], "pool": old_pool},
        fresh, n, today,
    )
    pool_by_id = {c["id"]: c for c in old_pool}
    for c in fresh:
        pool_by_id[c["id"]] = c
    picked_ids = {c["id"] for c in picks}
    used = set(state.get("used") or [])
    survivors = [
        c for c in pool_by_id.values()
        if c["id"] not in picked_ids and c["id"] not in used
    ]
    survivors.sort(key=lambda c: -c.get("weight", 0))
    state["pool"] = survivors[:150]
    state["used"].extend(new_used)
    state["picks"][today] = picks
    state["last_day"] = today
    if not dry_run:
        _save_state(state)
        print(f"[DAILY] {today}: {len(picks)} pick(s) persisted "
              f"(pool {len(state['pool'])} remaining, "
              f"{len(state['used'])} used total)\n")
    else:
        print(f"[DRY-RUN] {today}: would pick {len(picks)} (pool "
              f"{len(state['pool'])} remaining)\n")
    return picks


# ---------- download + backlog (optional) ----------
def download_and_backlog(picks: list[dict]) -> None:
    """Best-effort: download each picked demo, then build its backlog cards."""
    from downloader import is_already_downloaded, get_download_history  # type: ignore
    from models import DemoSource  # type: ignore

    for c in picks:
        mid = c["match_id"]
        demo = is_already_downloaded(mid, DemoSource.FACEIT)
        if demo:
            print(f"[DL] {mid} already on disk: {demo}")
        else:
            print(f"[DL] {mid} ({c['map']}) ...")
            try:
                r = subprocess.run(
                    [PY, str(ROOT / "main.py"), "faceit", "match", mid],
                    cwd=str(ROOT), timeout=1800, capture_output=True,
                    text=True, encoding="utf-8", errors="replace")
                out = r.stdout or ""
                if out:
                    print("\n".join(out.splitlines()[-4:]))
                if r.returncode != 0:
                    err = (r.stderr or "").strip().splitlines()[-1:] or ["download failed"]
                    print(f"  [ERR] download exited {r.returncode}: {err[0]}")
            except Exception as e:
                print(f"  [ERR] download failed: {e}")
            demo = is_already_downloaded(mid, DemoSource.FACEIT)
        if demo is None:
            # fallback: newest .dem (history may lag the browser scrape)
            candidates = sorted(DEMO_DIR.glob("*.dem"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
            demo = candidates[0] if candidates else None
        if not demo:
            print(f"  [ERR] demo not located for {mid}")
            continue
        print(f"  [BACKLOG] {demo.name}")
        try:
            map_name = str(c.get("map") or "").replace("de_", "").strip()
            if map_name:
                map_name = map_name[0].upper() + map_name[1:]
            cmd = [
                PY, str(ROOT / "scripts/faceit/extract_backlogs.py"),
                str(demo), "--player", str(c.get("player") or ""),
                "--match-id", mid, "--no-shorts",
            ]
            if map_name:
                cmd += ["--map", map_name]
            r = subprocess.run(
                cmd, cwd=str(ROOT), timeout=1200, capture_output=True, text=True)
            print("\n".join(r.stdout.splitlines()[-6:]))
        except Exception as e:
            print(f"  [ERR] backlog failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=DEFAULT_PICKS,
                    help=f"Picks per day (default {DEFAULT_PICKS})")
    ap.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                    help=f"Scrape window for fresh candidates (default {DEFAULT_HOURS}h)")
    ap.add_argument("--count", type=int, default=25, help="Matches per pro to scan")
    ap.add_argument("--force", action="store_true",
                    help="Re-scrape even if today already has picks")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report picks without persisting state")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="Print picks as JSON only")
    ap.add_argument("--download", action="store_true",
                    help="After picking: download each demo + build backlog cards")
    ap.add_argument("--skip-youtube-demand", action="store_true",
                    help="Do not scrape/recompute YouTube player demand weights")
    ap.add_argument("--replay-from", default=None, metavar="YYYY-MM-DD",
                    help="Walk this start day through --replay-to (report only)")
    ap.add_argument("--replay-to", default=None, metavar="YYYY-MM-DD",
                    help="End day for --replay-from (default: same as start)")
    ap.add_argument("--from-state", action="store_true",
                    help="Replay from rescored notable_daily.json instead of FACEIT")
    args = ap.parse_args()

    if args.replay_from:
        end = args.replay_to or args.replay_from
        if args.from_state:
            state = _load_state()
            seen: dict[str, dict] = {}
            for rec in list(state.get("pool") or []) + [
                x for ps in (state.get("picks") or {}).values() for x in ps
            ]:
                row = rescore_stored(rec)
                if row.get("id") and row.get("player"):
                    seen[row["id"]] = row
            candidates = list(seen.values())
            print(f"[REPLAY] {len(candidates)} rescored performances from state")
        else:
            if not args.skip_youtube_demand:
                try:
                    demand = refresh_player_demand(scrape=True)
                    print(
                        f"[DEMAND] {len(demand['index'])} players from "
                        f"{demand['window_videos']} videos "
                        f"(scraped {demand['scraped']} new/updated)"
                    )
                except Exception as exc:
                    print(f"[DEMAND] skipped ({exc})")
            start_d = datetime.strptime(args.replay_from, "%Y-%m-%d")
            as_of = datetime.strptime(end, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59,
            )
            hours = max(
                args.hours,
                int((as_of - (start_d - timedelta(days=3))).total_seconds() // 3600) + 1,
            )
            print(f"[REPLAY] scraping {hours}h through {end} ...")
            data = asyncio.run(collect(
                hours=hours, count=args.count, min_pros=2,
                perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
                today_only=False, exclude_today=False, as_of=as_of,
            ))
            candidates = data["candidates"]
            print(f"[REPLAY] {len(candidates)} live-scored performances")
        by_day = replay_days(candidates, args.replay_from, end, args.n)
        if args.as_json:
            print(json.dumps(
                {d: [{"player": c["player"], "kills": c.get("kills"),
                      "deaths": c.get("deaths"), "won": c.get("won"),
                      "weight": c.get("weight"), "map": c.get("map"),
                      "score": c.get("score")} for c in ps]
                 for d, ps in by_day.items()},
                indent=2, ensure_ascii=False,
            ))
            return
        for day, picks in by_day.items():
            print(f"\n=== {day} ({len(picks)} pick(s)) ===")
            if not picks:
                print("  (none)")
                continue
            for i, c in enumerate(picks, 1):
                print(f"PICK {i}/{len(picks)} ({c.get('stream')})")
                print(_format_pick(c))
        return

    today = datetime.now().strftime("%Y-%m-%d")
    picks = asyncio.run(pick_for_day(
        n=args.n, today=today, hours=args.hours, count=args.count,
        force=args.force, dry_run=args.dry_run,
        skip_youtube_demand=args.skip_youtube_demand,
    ))

    if args.as_json:
        print(json.dumps({"day": today, "picks": picks}, indent=2, ensure_ascii=False))
    else:
        if not picks:
            print(f"[WARN] no notable matches found in window; "
                  f"pool empty too — nothing picked today")
        else:
            for i, c in enumerate(picks, 1):
                print(f"PICK {i}/{len(picks)} ({c['stream']})")
                print(_format_pick(c))
                if picks.index(c) < len(picks) - 1:
                    print()

    if picks and args.download:
        download_and_backlog(picks)


if __name__ == "__main__":
    main()
