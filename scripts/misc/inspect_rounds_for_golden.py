"""Inspect kills in specific rounds and fix edit timeline manually to golden."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"

at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
et = json.loads((RUN / "edit_timeline.json").read_text(encoding="utf-8"))
kills = at["kills"]
rs = {r["round"]: r["tick"] for r in at["round_starts"]}
fe = {r["round"]: r["tick"] for r in at.get("round_freeze_ends", [])}
# Prefer next round_start - 1 as hard end (round_ends look buggy in this file).
next_rs = {}
rounds_sorted = sorted(rs.keys())
for i, rn in enumerate(rounds_sorted):
    if i + 1 < len(rounds_sorted):
        next_rs[rn] = rs[rounds_sorted[i + 1]] - 1
    else:
        # last round: use max kill tick + padding later
        next_rs[rn] = max(k["tick"] for k in kills if k["round"] == rn) + 640

HANDOFF = 160  # ~2.5s
POST_DEATH = 128  # 2s after death/kill for handoff
LEAD = 256
TAIL = 128
MIN_DUR = 640  # 10s at 64 tick


def dump_round(rn: int) -> None:
    print(f"\n=== round {rn} rs={rs.get(rn)} fe={fe.get(rn)} end={next_rs.get(rn)} ===")
    for i, k in enumerate(kills):
        if k["round"] == rn:
            print(f"  [{i}] t{k['tick']} {k['attacker']}>{k['victim']} ({k['weapon']})")


for rn in [11, 13, 16, 18, 21, 22, 23, 24, 26, 29, 30]:
    dump_round(rn)
