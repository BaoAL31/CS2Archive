"""Yield replay for the FACEIT solo-POV gate.

Rebuilds the demand payload as-of each day in [--from, --to] from the stored
YouTube scrape (same production builders: analyze_rows + build_index +
breakout_map), replays gate configs over that day's FACEIT candidates
(solo only, one per player+match), and tallies watchable solos/day.

Configs compare gate designs without touching production: ``demand-only``
(star without the breakout arm) vs ``breakout-or`` (the shipped rule:
demand rule OR a recent breakout video). Pass bands printed at the end
are advisory (exit 1 when the primary config misses them).

Views are today's captures aged back to each replay day (documented
time-travel caveat: old absolute bars understate, medians are stable).

Usage:
    python -m cs2archive.faceit.replay_gate
    python -m cs2archive.faceit.replay_gate --from 2026-09-15 --to 2026-10-01
    python -m cs2archive.faceit.replay_gate --out exports/pov_market/gate_replay.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cs2archive import scoring as _scoring
from cs2archive.faceit.scrape_notable import FACEIT_STAR_FLOOR, collect
from cs2archive.faceit.update_player_demand import (
    BREAKOUT_DAYS,
    BREAKOUT_PI,
    HISTORY_PATH,
    RECENT_DAYS,
    WINDOW_DAYS,
    build_index,
    breakout_map,
    canonical_player,
    in_window,
    load_history,
    load_seed_rows,
    recognised_aliases,
    refresh_velocity,
    upsert_history_rows,
)
from cs2archive.misc.analyze_pov_market import analyze_rows

ROOT = Path(__file__).resolve().parents[2]
OUT_DEFAULT = ROOT / "exports" / "pov_market" / "gate_replay.json"
# Advisory pass bands for the primary (breakout-or) config.
BAND_MEAN_LO, BAND_MEAN_HI = 0.9, 1.7
BAND_MAX_ZERO_SHARE = 6 / 17
BAND_MAX_OVER2 = 2


def _parse_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def rebuild_payload_at(rows: list[dict], at: datetime,
                       aliases: dict[str, str],
                       *, pi_floor: float = 0.0) -> dict:
    """Rebuild the demand payload using only rows published before ``at``.

    Mirrors ``update_player_demand.refresh()`` without the scrape: primary
    players are canonicalised first (exactly as refresh does — skipping
    this silently drops rich alias groups), ages are recomputed against
    ``at`` so velocities/PI are as-of that day. Views are still today's
    captures (time-travel caveat, documented in the module docstring).
    ``pi_floor`` is 0.0 (unfiltered maxes) so gate configs can apply
    their own breakout threshold; production filters at BREAKOUT_PI.
    """
    past = []
    for row in rows:
        if not _published_before(row.get("published_at"), at):
            continue
        player = canonical_player(row.get("primary_player") or "", aliases)
        if not player:
            continue
        kept = dict(row)
        kept["primary_player"] = player
        past.append(kept)
    aged = refresh_velocity(past, at)
    long_report = analyze_rows(
        in_window(aged, at, WINDOW_DAYS), source="replay-fit")
    recent_report = analyze_rows(
        in_window(aged, at, RECENT_DAYS), source="replay-recent")
    index, details = build_index(long_report, recent_report, aliases)
    if not index:
        raise RuntimeError(
            f"rebuilt index empty as-of {at.isoformat()} — refusing to "
            "fall back to the static research table")
    breakouts = breakout_map(in_window(aged, at, WINDOW_DAYS), aliases, at,
                             pi_floor=pi_floor)
    return {
        "updated_at": at.isoformat(),
        "index": index,
        "players": details,
        "breakouts": breakouts,
        "method": {"rule_version": _scoring.DEMAND_RULE_VERSION,
                   "breakout_days": BREAKOUT_DAYS, "breakout_pi": BREAKOUT_PI},
    }


def _published_before(raw: str | None, at: datetime) -> bool:
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < at


def _gate_demand_only(nick: str, payload_path: Path) -> tuple[bool, str]:
    # Replay-only: the payload is rebuilt as-of the replay day, so
    # freshness-vs-now is not enforced (same opt-out as roster_for_payload).
    star, _spike, index, _supported, _pi = _scoring.demand_eligibility(
        nick, payload_path, star_floor=FACEIT_STAR_FLOOR,
        enforce_freshness=False)
    if star:
        return True, f"star(ix={index})"
    return False, "reject"


def _gate_breakout_or(nick: str, payload_path: Path, *,
                      star_floor: float = FACEIT_STAR_FLOOR,
                      pi_floor: float = BREAKOUT_PI) -> tuple[bool, str]:
    # Replay-only: see _gate_demand_only on freshness.
    star, _spike, index, _supported, pi = _scoring.demand_eligibility(
        nick, payload_path, star_floor=star_floor,
        enforce_freshness=False)
    if star:
        return True, f"star(ix={index})"
    if pi is not None:
        try:
            if float(pi) >= pi_floor:
                return True, f"breakout(pi={pi})"
        except (TypeError, ValueError):
            pass
    return False, "reject"


def _gate_loose(nick: str, payload_path: Path) -> tuple[bool, str]:
    """One notch looser: star floor 1.25, breakout 75x."""
    return _gate_breakout_or(nick, payload_path, star_floor=1.25,
                             pi_floor=75.0)


CONFIGS = {
    "demand-only": _gate_demand_only,
    "breakout-or": _gate_breakout_or,
    "loose": _gate_loose,
}


def picks_for_day(candidates: list[dict], day: str,
                  gate) -> list[dict]:
    """Solo candidates dated ``day`` passing ``gate``, one per player+match."""
    win = [c for c in candidates
           if c.get("id") and str(c.get("date", ""))[:10] == day
           and c.get("stream") == "solo"]
    win.sort(key=lambda c: -c.get("weight", 0))
    picks, seen_players, seen_matches = [], set(), set()
    for c in win:
        if c["player"] in seen_players or c["match_id"] in seen_matches:
            continue
        ok, reason = gate(c["player"])
        if not ok:
            continue
        seen_players.add(c["player"])
        seen_matches.add(c["match_id"])
        picks.append({**c, "gate_reason": reason})
    return picks


def roster_for_payload(payload: dict) -> list[str]:
    """Eligible players under the breakout-or rule, from the payload alone.

    Uses the single shared predicate (``scoring.demand_eligibility``), so
    the roster can never disagree with the gate tallies.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False,
                                     encoding="utf-8") as handle:
        json.dump(payload, handle)
        path = Path(handle.name)
    try:
        nicks = {str(k).casefold()
                 for k in list(payload.get("index") or {})
                 + list(payload.get("breakouts") or {})}
        roster = []
        for nick in nicks:
            star, spike, _index, _supported, _pi = (
                _scoring.demand_eligibility(
                    nick, path, star_floor=FACEIT_STAR_FLOOR,
                    # B1: replay payloads are rebuilt as-of the replay day —
                    # freshness-vs-now is meaningless here; the replay's own
                    # time frame is the bound.
                    enforce_freshness=False))
            if star or spike:
                roster.append(nick)
        return sorted(roster)
    finally:
        path.unlink(missing_ok=True)


def run_replay(start: datetime, end: datetime, *, count: int = 60,
               configs: tuple[str, ...] = ("demand-only", "breakout-or",
                                            "loose"),
               candidates_path: Path | None = None) -> dict:
    """Collect once over the window, replay each day, return the tally.

    ``candidates_path`` caches the collect: when the file exists it is
    loaded instead of scraping, so configs are comparable across runs
    and any run is auditable after the fact.
    """
    aliases = recognised_aliases()
    history = upsert_history_rows(load_history(HISTORY_PATH), load_seed_rows())
    hours = int((end - (start - timedelta(days=3))).total_seconds() // 3600) + 1
    data = None
    if candidates_path is not None and candidates_path.exists():
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
        print(f"[replay] loaded {len(data['candidates'])} cached candidates "
              f"from {candidates_path}")
    if data is None:
        # collect() compares against a naive cutoff internally.
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                data = asyncio.run(collect(
                    hours=hours, count=count, min_pros=2,
                    perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
                    today_only=False, exclude_today=False,
                    as_of=end.replace(tzinfo=None),
                ))
                break
            except Exception as exc:  # transient FACEIT API flakes (timeout/404)
                last_exc = exc
                print(f"[replay] collect attempt {attempt}/3 failed "
                      f"({type(exc).__name__}); retrying ...")
        if data is None:
            raise RuntimeError(
                f"collect failed 3 times; last: {last_exc}") from last_exc
        if candidates_path is not None:
            candidates_path.parent.mkdir(parents=True, exist_ok=True)
            candidates_path.write_text(
                json.dumps(data, default=str), encoding="utf-8")
    candidates = data["candidates"]
    multi_today = sum(1 for c in candidates
                      if c.get("stream") == "multi")
    days, prev_rosters = [], {}
    day = start
    while day <= end:
        ds = day.strftime("%Y-%m-%d")
        at = day.replace(hour=23, minute=59, second=59)
        payload = rebuild_payload_at(history, at, aliases)
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False,
                encoding="utf-8") as handle:
            json.dump(payload, handle)
            payload_path = Path(handle.name)
        try:
            entry: dict = {"day": ds, "configs": {},
                           "roster": roster_for_payload(payload)}
            for name in configs:
                gate = CONFIGS[name]
                picks = picks_for_day(
                    candidates, ds,
                    lambda nick, _g=gate, _p=payload_path: _g(nick, _p))
                entry["configs"][name] = {
                    "count": len(picks),
                    "picks": [{k: p.get(k) for k in
                               ("player", "kills", "deaths", "kd", "adr",
                                "map", "weight", "gate_reason")}
                              for p in picks],
                }
        finally:
            payload_path.unlink(missing_ok=True)
        prev_rosters[ds] = entry["roster"]
        days.append(entry)
        day += timedelta(days=1)
    summary = {}
    for name in configs:
        counts = [d["configs"][name]["count"] for d in days]
        n = len(counts)
        summary[name] = {
            "total": sum(counts),
            "mean_per_day": round(sum(counts) / n, 2) if n else 0.0,
            "zero_days": sum(1 for v in counts if v == 0),
            "zero_share": round(sum(1 for v in counts if v == 0) / n, 3) if n else 0.0,
            "over2_days": sum(1 for v in counts if v > 2),
        }
    churn = {}
    keys = sorted(prev_rosters)
    for prev, cur in zip(keys, keys[1:]):
        before, after = set(prev_rosters[prev]), set(prev_rosters[cur])
        churn[cur] = sorted(before ^ after)
    return {
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
        "basis": ("solo-stream candidates only, no used-id suppression, "
                  "days are UTC labels while candidate dates are FACEIT "
                  "local stamps, payload rebuilt per day from stored CSV "
                  "with today's view captures"),
        "candidates": len(candidates),
        "multi_stream_candidates": multi_today,
        "days": days,
        "summary": summary,
        "roster_churn": churn,
    }


def check_bands(result: dict, config: str = "breakout-or") -> list[str]:
    """Advisory pass/fail lines for the primary config."""
    lines = []
    block = result["summary"][config]
    n = len(result["days"])
    mean = block["mean_per_day"]
    ok = BAND_MEAN_LO <= mean <= BAND_MEAN_HI
    lines.append(f"[{'PASS' if ok else 'FAIL'}] mean {mean}/day "
                 f"(band {BAND_MEAN_LO}-{BAND_MEAN_HI})")
    ok = block["zero_share"] <= BAND_MAX_ZERO_SHARE
    lines.append(f"[{'PASS' if ok else 'FAIL'}] zero days "
                 f"{block['zero_days']}/{n} (band <={BAND_MAX_ZERO_SHARE:.2f})")
    ok = block["over2_days"] <= BAND_MAX_OVER2
    lines.append(f"[{'PASS' if ok else 'FAIL'}] over-2 days "
                 f"{block['over2_days']} (band <={BAND_MAX_OVER2})")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="from_day", default=None,
                    help="start day YYYY-MM-DD (default: 17 days ending yesterday)")
    ap.add_argument("--to", dest="to_day", default=None,
                    help="end day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--count", type=int, default=60,
                    help="FACEIT matches per pro to scan (default 60)")
    ap.add_argument("--candidates-cache", type=Path, default=None,
                    help="cache collect JSON here (reuse across runs)")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT,
                    help="JSON report path (written unless --no-write)")
    ap.add_argument("--no-write", action="store_true",
                    help="print only, write no JSON")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    end = (_parse_day(args.to_day) if args.to_day
           else (now - timedelta(days=1)).replace(
               hour=0, minute=0, second=0, microsecond=0))
    start = (_parse_day(args.from_day) if args.from_day
             else end - timedelta(days=16))
    end = end.replace(hour=23, minute=59, second=59)
    print(f"[replay] {start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')} "
          f"(collect + per-day payload rebuild) ...")
    result = run_replay(start, end, count=args.count,
                        candidates_path=args.candidates_cache)
    for day in result["days"]:
        parts = " ".join(
            f"{name}={day['configs'][name]['count']}"
            for name in result["summary"])
        print(f"  {day['day']}: {parts}  roster={len(day['roster'])}")
    for name, block in result["summary"].items():
        print(f"[summary:{name}] total={block['total']} "
              f"mean={block['mean_per_day']}/day zero={block['zero_days']} "
              f"over2={block['over2_days']}")
    print("[bands:breakout-or]")
    failed = False
    for line in check_bands(result):
        print(f"  {line}")
        if line.startswith("[FAIL]"):
            failed = True
    if not args.no_write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[replay] wrote {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
