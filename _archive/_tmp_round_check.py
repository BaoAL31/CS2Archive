"""Check round data around tick 148185."""
import json, sys

at = json.load(open("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json"))

# Find kills around round containing tick 148185
kills = at["kills"]
round_ends = at["round_ends"]
round_starts = at["round_starts"]

print("=== Round starts ===")
for rs in round_starts:
    print(f"  round {rs['round']}: start_tick={rs['tick']}")

print("\n=== Round ends ===")
for re in round_ends:
    print(f"  round {re['round']}: end_tick={re['tick']}")

# Find which round contains tick 148185
target = 148185
for rs in sorted(round_starts, key=lambda x: x["tick"]):
    if rs["tick"] >= target:
        print(f"\nTick {target} is in or before round {rs['round']} (start at {rs['tick']})")
        break

# Get kills in the round before 148185
print("\n=== Kills near 148185 ===")
for k in kills:
    if abs(k["tick"] - 148185) < 5000:
        print(f"  r{k['round']} t{k['tick']} {k['attacker']:>15s} -> {k['victim']:<15s} {k['weapon']}")

print("\n=== All kills in the round containing 148185 ===")
for k in kills:
    if 140000 <= k["tick"] <= 150000:
        print(f"  r{k['round']} t{k['tick']} {k['attacker']:>15s} -> {k['victim']:<15s} {k['weapon']}")
