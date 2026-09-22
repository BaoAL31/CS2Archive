"""Count gate-qualified FACEIT POVs per day (leniency check for the listener)."""
import asyncio
import sys
from datetime import datetime, timedelta

sys.path.insert(0, "scripts")
sys.path.insert(0, "scripts/faceit")

from daily_notable import is_good_faceit_pov, _cand_day  # noqa: E402
from scrape_notable import collect  # noqa: E402

DAYS = sys.argv[1:] or ["2026-09-16", "2026-09-17"]


async def main() -> None:
    end_d = datetime.strptime(max(DAYS), "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    start_d = datetime.strptime(min(DAYS), "%Y-%m-%d") - timedelta(days=1)
    hours = int((end_d - start_d).total_seconds() // 3600) + 1
    print(f"[collect] {hours}h window")
    data = await collect(
        hours=hours, count=25, min_pros=2,
        perf_kd=1.5, perf_adr=100.0, perf_kills=30, perf_limit=120,
        today_only=False, exclude_today=False,
    )
    cands = data["candidates"]
    print(f"[collect] {len(cands)} scored performances total")
    for day in DAYS:
        ds = day
        ws = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        window = [c for c in cands if (cd := _cand_day(c)) and ws <= cd <= ds]
        good = [c for c in window if is_good_faceit_pov(c)]
        print(f"\n=== {day}: {len(window)} scored, {len(good)} qualify (gate) ===")
        for c in sorted(good, key=lambda c: -(c.get("weight") or 0)):
            print(
                f"  {c['player']:12s} {c.get('kills')}/{c.get('deaths'):>2}"
                f" K/D {c.get('kd')} ADR {c.get('adr')}"
                f" map {c.get('map'):8s} star {c.get('star_bonus')}"
                f" weight {c.get('weight')} {'WON' if c.get('won') else 'LOSS'}"
                f"  {c.get('match_id', '')[:24]}"
            )
        # near-misses: failed gate but decent line
        near = [c for c in window if c not in good and (c.get("kd") or 0) >= 1.2]
        print(f"  -- near-misses (K/D>=1.2 but no gate): {len(near)}")


asyncio.run(main())
