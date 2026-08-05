"""Check round_ends in action timeline."""
import json
at = json.load(open("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json"))
print("=== Last 5 round_ends ===")
for re in at["round_ends"][-5:]:
    print(f"  round {re['round']}: end_tick={re['tick']}")
print("=== Last 5 round_starts ===")
for rs in at["round_starts"][-5:]:
    print(f"  round {rs['round']}: start_tick={rs['tick']}")
print()
# Check round 15 specifically
re15 = [re for re in at["round_ends"] if re["round"] == 15]
rs15 = [rs for rs in at["round_starts"] if rs["round"] == 15]
rs16 = [rs for rs in at["round_starts"] if rs["round"] == 16]
print(f"Round 15: start={rs15[0]['tick'] if rs15 else 'N/A'}, end={re15[0]['tick'] if re15 else 'N/A'}")
print(f"Round 16: start={rs16[0]['tick'] if rs16 else 'N/A'}")
