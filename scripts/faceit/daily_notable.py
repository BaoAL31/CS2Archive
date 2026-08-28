"""
Daily FACEIT notable-match selector.

Runs once per day: calls `scrape_notable.collect()` (which already scores
every Recognised-Pro performance) and PICKS the top N (=3) for the day.
If the day's scrape cannot fill N, FALLS BACK to performances left over in
the persistent pool from previous days.

Idempotent: running twice on the same day returns the stored picks without
re-scraping (use --force to redo the day).

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
    python scripts/faceit/daily_notable.py --install-cron [--at 09:00]
    python scripts/faceit/daily_notable.py --remove-cron
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

from scrape_notable import collect

STATE_FILE = ROOT / ".data" / "notable_daily.json"
DEMO_DIR = ROOT / "demos" / "faceit"
CRON_TASK = "CS2ArchiveFaceitDaily"
DEFAULT_TIME = "09:00"
PY = sys.executable


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


# ---------- download + backlog (optional) ----------
def _download_and_backlog(picks: list[dict]) -> None:
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
                    cwd=str(ROOT), timeout=1800, capture_output=True, text=True)
                print("\n".join(r.stdout.splitlines()[-4:]))
            except Exception as e:
                print(f"  [ERR] download failed: {e}")
                continue
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
            r = subprocess.run(
                [PY, str(ROOT / "scripts/faceit/create_faceit_match_backlog.py"),
                 str(demo), "--match-id", mid],
                cwd=str(ROOT), timeout=1200, capture_output=True, text=True)
            print("\n".join(r.stdout.splitlines()[-6:]))
        except Exception as e:
            print(f"  [ERR] backlog failed: {e}")


# ---------- cron registration ----------
def install_cron(at: str) -> None:
    script = str((ROOT / "scripts" / "faceit" / "daily_notable.py").resolve())
    tr = f'"{PY}" "{script}"'
    cmd = ["schtasks", "/Create", "/F", "/TN", CRON_TASK, "/SC", "DAILY", "/ST", at,
           "/TR", tr, "/RL", "LIMITED"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip() or "", flush=True)
    print(("SUCCESS" if r.returncode == 0 else f"FAIL({r.returncode})")
          + f" -> {CRON_TASK} daily at {at} ->\n    {tr}")
    if r.returncode != 0:
        sys.exit(1)


def remove_cron() -> None:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", CRON_TASK],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip() or "", flush=True)
    print(("SUCCESS" if r.returncode == 0 else f"FAIL({r.returncode})")
          + f" -> removed {CRON_TASK}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=3, help="Picks per day (default 3)")
    ap.add_argument("--hours", type=int, default=72,
                    help="Scrape window for fresh candidates (default 72h)")
    ap.add_argument("--count", type=int, default=25, help="Matches per pro to scan")
    ap.add_argument("--force", action="store_true",
                    help="Re-scrape even if today already has picks")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report picks without persisting state")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="Print picks as JSON only")
    ap.add_argument("--download", action="store_true",
                    help="After picking: download each demo + build backlog cards")
    ap.add_argument("--install-cron", action="store_true",
                    help="Register a Windows daily scheduled task")
    ap.add_argument("--at", default=DEFAULT_TIME, help="Cron run time HH:MM (default 09:00)")
    ap.add_argument("--remove-cron", action="store_true",
                    help="Remove the scheduled task")
    args = ap.parse_args()

    if args.remove_cron:
        remove_cron()
        return
    if args.install_cron:
        install_cron(args.at)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()

    # idempotent per-day unless --force; also re-run if stored picks are from
    # the old match-level schema (no 'player' field).
    prev = state["picks"].get(today)
    if (
        not args.force
        and state.get("last_day") == today
        and prev
        and all(c.get("score_version") == 2 for c in prev)
    ):
        picks = prev
    else:
        data = asyncio.run(collect(
            hours=args.hours, count=args.count, min_pros=2,
            perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
            today_only=False, exclude_today=False,
        ))
        fresh = data["candidates"]
        # Refresh the persistent pool: key on performance id (match_id:player),
        # drop pre-schema match-level entries, add/overwrite fresh ones.
        pool_by_id = {
            c["id"]: c
            for c in state["pool"]
            if c.get("id") and c.get("score_version") == 2
        }
        for c in fresh:
            pool_by_id[c["id"]] = c
        state["pool"] = list(pool_by_id.values())

        picks, new_used, survivors = select(state, state["pool"], args.n, today)
        # bound the fallback pool so stale entries can't accumulate forever
        survivors.sort(key=lambda c: -c.get("weight", 0))
        state["pool"] = survivors[:150]
        state["used"].extend(new_used)
        state["picks"][today] = picks
        state["last_day"] = today
        if not args.dry_run:
            _save_state(state)
            print(f"[DAILY] {today}: {len(picks)} pick(s) persisted "
                  f"(pool {len(state['pool'])} remaining, "
                  f"{len(state['used'])} used total)\n")
        else:
            print(f"[DRY-RUN] {today}: would pick {len(picks)} (pool "
                  f"{len(state['pool'])} remaining)\n")

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
        _download_and_backlog(picks)


if __name__ == "__main__":
    main()
