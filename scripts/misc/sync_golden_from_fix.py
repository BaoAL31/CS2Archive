"""Write golden fixture from fix-pass output (ticks + types aligned)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
FIXTURES = ROOT / "scripts" / "highlights" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

from highlights.build_edit_timeline import (  # noqa: E402
    _extract_players_from_action_timeline,
    _fix_edit_timeline,
)

at = json.loads((RUN / "action_timeline.json").read_text(encoding="utf-8"))
rendered = json.loads((RUN / "edit_timeline.rendered_73.json").read_text(encoding="utf-8"))
players = _extract_players_from_action_timeline(at)
fixed = _fix_edit_timeline({"segments": rendered["segments"]}, at, players)

out_et = {
    "demo_path": rendered.get("demo_path") or at.get("demo_path"),
    "map": rendered.get("map") or at.get("map"),
    "segments": fixed["segments"],
}
(RUN / "edit_timeline.json").write_text(json.dumps(out_et, indent=2), encoding="utf-8")
(RUN / "edit_timeline.golden.json").write_text(json.dumps(out_et, indent=2), encoding="utf-8")

goal = {
    "description": (
        "Golden edit timeline after manual review (Cache FACEIT). "
        "Equal/higher streak interrupt, 10s min, death+2s, last-seg round-end expand."
    ),
    "segments": [
        {
            "start_tick": s["start_tick"],
            "end_tick": s["end_tick"],
            "pov_steam_id": s["pov_steam_id"],
            "segment_type": s["segment_type"],
            "kill_indices": s["kill_indices"],
        }
        for s in fixed["segments"]
    ],
}
(FIXTURES / "cache_full_goal_segments.json").write_text(json.dumps(goal, indent=2), encoding="utf-8")
print(f"Synced golden from fix-pass: {len(goal['segments'])} segments")
