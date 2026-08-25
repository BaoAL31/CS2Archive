"""
Daily FACEIT notable-match selector.

Runs once per day: scrapes notable FACEIT matches (multi-pro + single-pro
standout), ranks them, and PICKS the top N (=3) matches for the day. If the
day's scrape cannot fill N good matches, it FALLS BACK to notable matches
left over in the persistent pool from previous days.

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

from scrape_notable import collect, _num  # noqa: E402

STATE_FILE = ROOT / ".data" / "notable_daily.json"
DEMO_DIR = ROOT / "demos" / "faceit"
CRON_TASK = "CS2ArchiveFaceitDaily"
DEFAULT_TIME = "09:00"
PY = sys.executable


# ---------- scoring ----------
def _best_kd(rec: dict) -> float:
    if rec.get("players"):
        return max((_num(l.get("kd"), float) for l in rec["players"].values()), default=0.0)
    return _num(rec.get("line", {}).get("kd"), float)


def make_candidate(rec: dict, stream: str) -> dict:
    """Normalize a scrape record into a persistable pool/pick candidate."""
    is_multi = stream == "multi"
    pros = rec.get("pros", []) if is_multi else [rec.get("pro", "?")]
    kd = _best_kd(rec)
    weight = int(kd * 1000)
    if is_multi:
        weight += len(pros) * 100_000 + 1_000_000
    else:
        weight += 10_000  # solo standouts sort behind every multi-pro match
    date = rec.get("date")
    cand = {
        "match_id": rec["id"],
        "stream": stream,
        "map": rec.get("map", "?"),
        "score": rec.get("score", ""),
        "date": date.strftime("%Y-%m-%d") if date else None,
        "pros": pros,
        "best_kd": round(kd, 2),
        "avg_elo": rec.get("avg_elo"),
        "weight": weight,
    }
    if not is_multi:
        cand["pro"] = rec.get("pro")
        line = rec.get("line", {})
        cand["kills"] = line.get("kills")
        cand["deaths"] = line.get("deaths")
        cand["adr"] = line.get("adr")
        cand["hs"] = line.get("hs")
    return cand


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


def _reason(cand: dict) -> str:
    if cand["stream"] == "multi":
        return f"{len(cand['pros'])} pros, best K/D {cand['best_kd']}, avg ELO {cand.get('avg_elo') or 'n/a'}"
    return (f"solo standout {cand.get('pro')} {cand.get('kills')}/{cand.get('deaths')} "
            f"(K/D {cand['best_kd']}, ADR {cand.get('adr')})")


def select(state: dict, candidates: list[dict], n: int, today: str) -> tuple[list[dict], list[str], list[dict]]:
    """Pick up to n best candidates, skipping already-used match ids.

    Returns (picks, newly_used_ids, survivors). Candidates include fresh
    scrape + the persistent pool from previous days (fallback).
    """
    used = set(state.get("used", []))
    fresh = [c for c in candidates if c["match_id"] not in used]
    fresh.sort(key=lambda c: (-c["weight"],
                              -(datetime.strptime(c["date"], "%Y-%m-%d").timestamp()
                                if c.get("date") else 0)))
    picks, taken = [], set()
    for c in fresh:
        if len(picks) >= n:
            break
        if c["match_id"] in taken:
            continue
        taken.add(c["match_id"])
        picks.append(c)
    new_used = list(taken)
    # survivors stay in the pool for future days
    survivors = [c for c in candidates if c["match_id"] not in taken]
    return picks, new_used, survivors


def _format_pick(c: dict) -> str:
    when = c.get("date") or "?"
    return (f"  * {c['match_id']}\n"
            f"      {len(c['pros'])} pro(s) {', '.join(c['pros'])} "
            f"{c['map']} {c['score']} {when}\n"
            f"      {_reason(c)}")


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

    # idempotent per-day unless --force
    if not args.force and state.get("last_day") == today and state["picks"].get(today):
        picks = state["picks"][today]
    else:
        data = asyncio.run(collect(
            hours=args.hours, count=args.count, min_pros=2,
            perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
            today_only=False, exclude_today=False,
        ))
        fresh = ([make_candidate(r, "multi") for r in data["multi"]] +
                 [make_candidate(r, "solo") for r in data["solo"]])
        # seed the persistent pool with any fresh candidates it lacks
        seen = {c["match_id"] for c in state["pool"]}
        for c in fresh:
            if c["match_id"] not in seen:
                state["pool"].append(c)
                seen.add(c["match_id"])

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
