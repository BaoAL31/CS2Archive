"""Score rebuilt edit_timeline.json against cache_full_goal_segments.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
FIX = ROOT / "scripts" / "highlights" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()


def main() -> None:
    from highlights.build_edit_timeline import WARMUP_ROUND, _longest_lived_sid

    at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
    kills = at["kills"]
    rebuilt = json.loads((RUN / "edit_timeline.json").read_text(encoding="utf-8"))["segments"]
    golden = json.loads((FIX / "cache_full_goal_segments.json").read_text(encoding="utf-8"))["segments"]

    def kset(s):
        return frozenset(s["kill_indices"])

    # Soft-match warmup: only require one r0 segment with longest-lived POV
    def is_warmup(s):
        return kills[s["kill_indices"][0]]["round"] == WARMUP_ROUND

    want_live = [s for s in golden if not is_warmup(s)]
    got_live = [s for s in rebuilt if not is_warmup(s)]
    want_wu = [s for s in golden if is_warmup(s)]
    got_wu = [s for s in rebuilt if is_warmup(s)]

    got = {kset(s): s for s in got_live}
    want = {kset(s): s for s in want_live}

    missing = sorted(want.keys() - got.keys(), key=min)
    extra = sorted(got.keys() - want.keys(), key=min)
    shared = want.keys() & got.keys()

    exact_ticks = 0
    tick_errs = []
    for fs in sorted(shared, key=min):
        g, w = got[fs], want[fs]
        if g["start_tick"] == w["start_tick"] and g["end_tick"] == w["end_tick"] and g["pov_steam_id"] == w["pov_steam_id"]:
            exact_ticks += 1
        else:
            tick_errs.append((w, g))

    want_order = [kset(s) for s in want_live]
    got_order = [kset(s) for s in got_live]
    n, m = len(want_order), len(got_order)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            if want_order[i] == got_order[j]:
                dp[i + 1][j + 1] = dp[i][j] + 1
            else:
                dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])
    lcs = dp[n][m]

    print("=" * 60)
    print("GOLDEN SCORECARD (rebuild vs cache_full_goal) — warmup soft")
    print("=" * 60)
    print(f"golden live segments:  {len(want_live)} (+{len(want_wu)} warmup)")
    print(f"rebuilt live segments: {len(got_live)} (+{len(got_wu)} warmup)")
    if got_wu:
        wu = got_wu[0]
        expect_pov = _longest_lived_sid(kills, wu["kill_indices"])
        wu_ok = len(got_wu) == 1 and wu["pov_steam_id"] == expect_pov
        print(f"warmup OK: {wu_ok} (segs={len(got_wu)}, pov=...{wu['pov_steam_id'][-6:]}, expect=...{expect_pov[-6:]}, kis={len(wu['kill_indices'])} kills)")
    print()
    print(f"kill-set precision: {len(shared)}/{len(got)} = {100*len(shared)/max(len(got),1):.1f}%")
    print(f"kill-set recall:    {len(shared)}/{len(want)} = {100*len(shared)/max(len(want),1):.1f}%")
    f1 = 0.0
    if shared:
        p = len(shared) / len(got)
        r = len(shared) / len(want)
        f1 = 2 * p * r / (p + r)
    print(f"kill-set F1 (live): {100*f1:.1f}%")
    print(f"exact tick+pov:     {exact_ticks}/{len(shared)} shared ({100*exact_ticks/max(len(shared),1):.1f}% of shared)")
    print(f"order LCS:          {lcs}/{len(want)} golden live order covered")
    print()

    if missing:
        print(f"MISSING from rebuild ({len(missing)}):")
        for fs in missing:
            s = want[fs]
            ki = s["kill_indices"]
            rn = kills[ki[0]]["round"]
            print(f"  r{rn} kis={ki} {kills[ki[0]]['attacker']} t{s['start_tick']}-{s['end_tick']}")
        print()

    if extra:
        print(f"EXTRA in rebuild ({len(extra)}):")
        for fs in extra:
            s = got[fs]
            ki = s["kill_indices"]
            rn = kills[ki[0]]["round"]
            print(f"  r{rn} kis={ki} {kills[ki[0]]['attacker']} t{s['start_tick']}-{s['end_tick']}")
        print()

    if tick_errs:
        print(f"TICK/POV mismatches on shared kill-sets ({len(tick_errs)}):")
        for w, g in tick_errs[:20]:
            print(
                f"  kis={w['kill_indices']}: "
                f"got t{g['start_tick']}-{g['end_tick']} pov=...{g['pov_steam_id'][-6:]} | "
                f"want t{w['start_tick']}-{w['end_tick']} pov=...{w['pov_steam_id'][-6:]}"
            )
        print()

    print("Per-round kill-set agreement (live rounds only):")
    rounds = sorted({kills[ki]["round"] for s in want_live + got_live for ki in s["kill_indices"]})
    for rn in rounds:
        wsets = {kset(s) for s in want_live if kills[s["kill_indices"][0]]["round"] == rn}
        gsets = {kset(s) for s in got_live if kills[s["kill_indices"][0]]["round"] == rn}
        ok = wsets == gsets
        mark = "OK" if ok else "DIFF"
        if not ok:
            print(f"  r{rn:02d} {mark}  want={sorted(map(sorted, wsets))} got={sorted(map(sorted, gsets))}")
        else:
            print(f"  r{rn:02d} {mark}")

if __name__ == "__main__":
    main()
