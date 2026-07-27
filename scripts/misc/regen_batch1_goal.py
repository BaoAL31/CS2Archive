"""Regenerate cache_batch1_goal_segments.json from current _fix_edit_timeline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _extract_players_from_action_timeline,
    _fix_edit_timeline,
    _load_action_timeline,
)

at = _load_action_timeline(FIXTURES / "cache_batch1_action_timeline.json")
players = _extract_players_from_action_timeline(at)
llm = {"segments": json.loads((FIXTURES / "cache_batch1_llm_segments.json").read_text())["segments"]}
fixed = _fix_edit_timeline(llm, at, players)["segments"]

# Keep same length as previous goal if it was truncated to batch1 rounds only
old = json.loads((FIXTURES / "cache_batch1_goal_segments.json").read_text())
n = len(old["segments"])
goal = {
    "description": old.get("description", "Batch1 golden after fix-pass"),
    "segments": [
        {
            "start_tick": s["start_tick"],
            "end_tick": s["end_tick"],
            "pov_steam_id": s["pov_steam_id"],
            "segment_type": s["segment_type"],
            "kill_indices": s["kill_indices"],
        }
        for s in fixed[:n]
    ],
}
(FIXTURES / "cache_batch1_goal_segments.json").write_text(json.dumps(goal, indent=2) + "\n", encoding="utf-8")
print(f"Wrote {len(goal['segments'])} batch1 goal segments")
for i, s in enumerate(goal["segments"], 1):
    print(f"  {i} t{s['start_tick']}-{s['end_tick']} ({(s['end_tick']-s['start_tick'])/64:.1f}s) kis={s['kill_indices']}")
