"""Dump edit timeline segments with kill detail for manual golden editing."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
run = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
et = json.loads((run / "edit_timeline.json").read_text(encoding="utf-8"))
at = json.loads((run / "action_timeline.json").read_text(encoding="utf-8"))
kills = at["kills"]
re_map = {r["round"]: r["tick"] for r in at.get("round_ends", [])}
rs_map = {r["round"]: r["tick"] for r in at.get("round_starts", [])}
fe_map = {r["round"]: r["tick"] for r in at.get("round_freeze_ends", [])}

lo = int(sys.argv[1]) if len(sys.argv) > 1 else 1
hi = int(sys.argv[2]) if len(sys.argv) > 2 else len(et["segments"])

for i in range(lo, hi + 1):
    s = et["segments"][i - 1]
    kis = s["kill_indices"]
    parts = []
    for ki in kis:
        kk = kills[ki]
        parts.append(
            f"{ki}:{kk['attacker']}>{kk['victim']}@{kk['tick']}({kk['weapon']})r{kk['round']}"
        )
    rn = kills[kis[0]]["round"] if kis else None
    dur = s["end_tick"] - s["start_tick"]
    print(
        f"seg-{i:03d} t{s['start_tick']}-{s['end_tick']} "
        f"({dur}t/{dur/64:.1f}s) pov={s['pov_steam_id'][-6:]} "
        f"r={rn} rs={rs_map.get(rn)} fe={fe_map.get(rn)} re={re_map.get(rn)}"
    )
    print("  " + " | ".join(parts))
