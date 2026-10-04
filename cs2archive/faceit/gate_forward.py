"""Forward kill metrics for gate variants: per-render performance.

Two yardsticks, deliberately separated:

- ``perf_ratio`` (competitor-relative): our upload's views/day divided by
  the player's competitor-channel median. Comparable across players but
  punishes a small channel structurally (our median is ~0.4 views/day
  against competitors' 5-38).
- ``own_pi`` (own-channel-relative): views/day divided by OUR channel
  median. This is the kill metric: keep >= 2.0 / hold 1.0-2.0 /
  kill < 1.0, read as the median over ~10 breakout-qualified renders.

Unaliased rows (no Recognised Pro) are skipped everywhere: they are
utility content wells, not POV evidence.

Usage:
    python -m cs2archive.faceit.gate_forward --player donk --video-id <id>
    python -m cs2archive.faceit.gate_forward --table
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
OWN_HISTORY = ROOT / "exports" / "pov_market" / "own" / "video_history.csv"
OWN_CHANNEL = "CS2 Archive"
VELOCITY_FLOOR_DAYS = 7.0
WINDOW_DAYS = 180

from cs2archive.faceit.update_player_demand import (  # noqa: E402
    load_seed_rows,
    recognised_aliases,
)


def _parse_pub(value: str | None) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _vpd(row: dict, now: datetime) -> tuple[float, float] | None:
    pub = _parse_pub(row.get("published_at"))
    if pub is None:
        return None
    age = max((now - pub).total_seconds() / 86400, 1 / 24)
    try:
        views = float(row.get("views") or 0)
    except (TypeError, ValueError):
        return None
    return views / age, age


def own_channel_rows(own_rows: list[dict] | None = None) -> list[dict]:
    """Fresh own-channel rows: main seed union first, own CSV as fallback."""
    if own_rows is not None:
        return [r for r in own_rows if (r.get("channel") or "") == OWN_CHANNEL]
    fresh = [r for r in _deduped_seed_rows()
             if (r.get("channel") or "") == OWN_CHANNEL]
    if fresh:
        return fresh
    return [r for r in _read_csv(OWN_HISTORY)
            if (r.get("channel") or "") in (OWN_CHANNEL, "")]


def own_channel_median_vpd(*, now: datetime | None = None,
                           min_age_days: float = 7.0,
                           own_rows: list[dict] | None = None) -> dict:
    """Median views/day of our own long-form channel (the kill denominator)."""
    now = now or datetime.now(timezone.utc)
    velocities = []
    for row in own_channel_rows(own_rows):
        try:
            if float(row.get("duration_seconds") or 0) < 300:
                continue
        except (TypeError, ValueError):
            continue
        parsed = _vpd(row, now)
        if parsed is None:
            continue
        _vpd_raw, age = parsed
        if age < min_age_days:
            continue
        try:
            views = float(row.get("views") or 0)
        except (TypeError, ValueError):
            continue
        velocities.append(views / max(age, VELOCITY_FLOOR_DAYS))
    return {
        "videos": len(velocities),
        "median_vpd": round(median(velocities), 2) if velocities else 0.0,
    }


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _deduped_seed_rows() -> list[dict]:
    """Seed union deduped by video_id (newest capture wins), own channel out.

    Raw seed CSVs overlap heavily (re-exports across folders); without
    dedupe the median is a weighted multiset. Own uploads are excluded:
    they are the numerator, never the baseline.
    """
    latest: dict[str, dict] = {}
    for row in sorted(load_seed_rows(),
                      key=lambda r: str(r.get("captured_at") or "")):
        if (row.get("channel") or "") == OWN_CHANNEL:
            continue
        video_id = str(row.get("video_id") or "").strip()
        if video_id:
            latest[video_id] = row
    return list(latest.values())


def competitor_median_vpd(player: str, *, now: datetime | None = None,
                          min_age_days: float = 7.0,
                          min_duration_s: float = 300.0,
                          window_days: float = WINDOW_DAYS,
                          rows: list[dict] | None = None) -> dict:
    """Median floored views/day of competitor long-form videos for ``player``."""
    now = now or datetime.now(timezone.utc)
    aliases = recognised_aliases()
    want = str(player).casefold()
    velocities = []
    for row in _deduped_seed_rows() if rows is None else rows:
        if (row.get("channel") or "") == OWN_CHANNEL:
            continue
        raw = (row.get("primary_player") or "").strip()
        canon = aliases.get(raw.casefold()) if raw else None
        if canon is None or canon.casefold() != want:
            continue
        try:
            if float(row.get("duration_seconds") or 0) < min_duration_s:
                continue
        except (TypeError, ValueError):
            continue
        pub = _parse_pub(row.get("published_at"))
        if pub is None or (now - pub).days > window_days:
            continue
        parsed = _vpd(row, now)
        if parsed is None:
            continue
        _vpd_raw, age = parsed
        if age < min_age_days:
            continue
        try:
            views = float(row.get("views") or 0)
        except (TypeError, ValueError):
            continue
        velocities.append(views / max(age, VELOCITY_FLOOR_DAYS))
    return {
        "player": player,
        "videos": len(velocities),
        "median_vpd": round(median(velocities), 1) if velocities else 0.0,
    }


def own_video_pi(video_id: str, *, now: datetime | None = None,
                 own_rows: list[dict] | None = None,
                 comp_rows: list[dict] | None = None) -> dict:
    """Our upload's performance: competitor ratio + own-channel PI."""
    now = now or datetime.now(timezone.utc)
    target = None
    for row in own_channel_rows(own_rows):
        if str(row.get("video_id") or "").strip() == video_id:
            target = row
            break
    if target is None:
        return {"video_id": video_id, "error": "not in own history"}
    aliases = recognised_aliases()
    raw = (target.get("primary_player") or "").strip()
    player = aliases.get(raw.casefold()) if raw else None
    if not player:
        return {"video_id": video_id, "error": "unaliased player"}
    parsed = _vpd(target, now)
    if parsed is None:
        return {"video_id": video_id, "player": player, "error": "no views/date"}
    _vpd_raw, age = parsed
    if age < VELOCITY_FLOOR_DAYS:
        return {"video_id": video_id, "player": player,
                "error": f"younger than {VELOCITY_FLOOR_DAYS}d: not measurable"}
    try:
        views = float(target.get("views") or 0)
    except (TypeError, ValueError):
        return {"video_id": video_id, "player": player, "error": "no views"}
    vpd = views / max(age, VELOCITY_FLOOR_DAYS)
    base = competitor_median_vpd(player, now=now,
                                 rows=None if comp_rows is None else comp_rows)
    median_vpd = base["median_vpd"]
    own_med = own_channel_median_vpd(
        now=now, own_rows=None if own_rows is None else own_channel_rows(own_rows))
    own_median = own_med["median_vpd"]
    return {
        "video_id": video_id,
        "player": player,
        "age_days": round(age, 1),
        "views": target.get("views"),
        "vpd": round(vpd, 1),
        "competitor_median_vpd": median_vpd,
        "competitor_videos": base["videos"],
        "perf_ratio": round(vpd / median_vpd, 2) if median_vpd else None,
        "own_median_vpd": own_median,
        "own_pi": round(vpd / own_median, 2) if own_median else None,
    }


def own_table(*, now: datetime | None = None,
              own_rows: list[dict] | None = None) -> list[dict]:
    """Every own long-form video (age >= 7d) with perf_ratio + own_pi."""
    now = now or datetime.now(timezone.utc)
    comp_rows = _deduped_seed_rows()
    own_med = own_channel_median_vpd(now=now, own_rows=own_rows)
    own_median = own_med["median_vpd"]
    out = []
    for row in own_channel_rows(own_rows):
        try:
            if float(row.get("duration_seconds") or 0) < 300:
                continue
        except (TypeError, ValueError):
            continue
        parsed = _vpd(row, now)
        if parsed is None:
            continue
        _vpd_raw, age = parsed
        if age < VELOCITY_FLOOR_DAYS:
            continue
        aliases = recognised_aliases()
        raw = (row.get("primary_player") or "").strip()
        player = aliases.get(raw.casefold()) if raw else None
        if not player:
            continue
        try:
            views = float(row.get("views") or 0)
        except (TypeError, ValueError):
            continue
        vpd = views / max(age, VELOCITY_FLOOR_DAYS)
        base = competitor_median_vpd(player, now=now, rows=comp_rows)
        out.append({
            "video_id": str(row.get("video_id") or ""),
            "player": player,
            "age_days": round(age, 1),
            "vpd": round(vpd, 1),
            "competitor_median_vpd": base["median_vpd"],
            "perf_ratio": (round(vpd / base["median_vpd"], 2)
                           if base["median_vpd"] else None),
            "own_median_vpd": own_median,
            "own_pi": (round(vpd / own_median, 2) if own_median else None),
        })
    out.sort(key=lambda r: (r["own_pi"] is None, -(r["own_pi"] or 0)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--player", default=None)
    ap.add_argument("--video-id", default=None)
    ap.add_argument("--table", action="store_true",
                    help="all own long-form videos with perf_ratio")
    args = ap.parse_args(argv)
    if args.table:
        rows = own_table()
        for r in rows:
            print(f"{r['own_pi']}  {r['player']:12} {r['video_id']} "
                  f"vpd={r['vpd']} own_med={r.get('own_median_vpd')} "
                  f"perf_ratio={r['perf_ratio']}")
        return 0
    if args.video_id:
        print(own_video_pi(args.video_id))
        return 0
    if args.player:
        print(competitor_median_vpd(args.player))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
