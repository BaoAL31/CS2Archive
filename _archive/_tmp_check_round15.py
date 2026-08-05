"""Debug round 15 end_tick in action timeline."""
import json
at = json.load(open("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json"))
for re in at["round_ends"]:
    if re["round"] in (14, 15, 16):
        print(f"  round {re['round']}: end_tick={re['tick']}")
# Also verify the build_short_timeline code path:
# build_short_timeline_from_action does:
round_ends_dict = {re["round"]: re["tick"] for re in at["round_ends"]}
print(f"\nround_ends dict: {round_ends_dict.get(15, 'MISSING')}")
print(f"14 in dict: {14 in round_ends_dict} -> {round_ends_dict.get(14)}")
print(f"15 in dict: {15 in round_ends_dict} -> {round_ends_dict.get(15)}")
print(f"16 in dict: {16 in round_ends_dict} -> {round_ends_dict.get(16)}")
