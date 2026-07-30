"""Debug detect_shorts round handling for the JT 2v5 short."""
import json, sys
sys.path.insert(0, "scripts")
from _pathsetup import ensure; ensure()

from pathlib import Path
from shorts.build_short_timeline import build_short_timeline_from_action

at_path = Path("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json")
demo_path = Path("demos/hltv/2396004-liquid-vs-vitality-blast-bounty-2026-season-2/liquid-vs-vitality-m1-anubis.dem")

result = build_short_timeline_from_action(at_path, demo_path)

for s in result["shorts"]:
    if s.get("pov_nick") == "JT" and s["short_type"] == "clutch":
        print(f"JT 2v5:")
        print(f"  start_tick: {s['start_tick']}")
        print(f"  end_tick: {s['end_tick']}")
        print(f"  round_win_tick: {s.get('round_win_tick')}")
        print(f"  win_event: {s.get('win_event')}")
        print(f"  clutch_initial_count: {s.get('clutch_initial_count')}")
        # Trace the round
        # Find round for start_tick
        at = json.load(open(at_path))
        for rs in at["round_starts"]:
            if rs["tick"] <= s['start_tick']:
                continue
            print(f"  Round {rs['round']} starts at {rs['tick']}")
            break
        for re in at["round_ends"]:
            if abs(re['tick'] - s.get('round_win_tick', 0)) < 100:
                print(f"  Nearby round_end: round {re['round']} tick={re['tick']}")
