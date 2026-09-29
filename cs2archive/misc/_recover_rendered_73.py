"""Rebuild rendered 73-seg kill sets from MP4 filenames + action timeline."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"

at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
kills = at["kills"]
parsed = []
for p in sorted((RUN / "segments").glob("seg-*.mp4")):
    m = re.match(r"seg-(\d+)-pov-(\d+)-tick-(\d+)-to-(\d+)", p.name)
    if not m:
        continue
    n, pov, a, b = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
    # kills by this POV whose tick is inside [start, end]
    kis = [
        i
        for i, k in enumerate(kills)
        if str(k["attacker_steam_id"]) == pov and a <= int(k["tick"]) <= b
    ]
    # also include kills slightly outside end (anchoring can truncate)
    if not kis:
        kis = [
            i
            for i, k in enumerate(kills)
            if str(k["attacker_steam_id"]) == pov and a - 64 <= int(k["tick"]) <= b + 256
        ]
    rn = kills[kis[0]]["round"] if kis else None
    parsed.append({"n": n, "pov": pov, "start": a, "end": b, "kis": kis, "round": rn})

out = {
    "demo_path": at.get("demo_path"),
    "map": at.get("map_name") or at.get("map"),
    "segments": [
        {
            "start_tick": s["start"],
            "end_tick": s["end"],
            "pov_steam_id": s["pov"],
            "segment_type": "multi_kill" if len(s["kis"]) >= 2 else "default",
            "kill_indices": s["kis"],
            "rationale": f"recovered from render seg-{s['n']:03d}",
        }
        for s in parsed
    ],
}
(RUN / "edit_timeline.rendered_73.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"Recovered {len(parsed)} segments")
for s in parsed:
    if s["n"] in {27, 28, 32, 38, 39, 41, 42, 43, 47, 48, 49, 50, 51, 54, 55, 56, 57, 60, 61, 62, 68, 69, 70, 71, 72, 73}:
        print(
            f"  seg-{s['n']:03d} r{s['round']} t{s['start']}-{s['end']} "
            f"kis={s['kis']} pov=...{s['pov'][-6:]}"
        )
# warn empty
empty = [s for s in parsed if not s["kis"]]
print(f"empty kis: {len(empty)}")
for s in empty[:10]:
    print(" ", s)
