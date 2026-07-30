"""Verify mezii 2v5 clutch at tick 85348."""
import json
at = json.load(open("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json"))

# Find round (85348 is before round 9 start at 87765? Wait no - round 8 starts earlier)
# Let me find which round starts <= 85348
for rs in at["round_starts"]:
    if rs["tick"] <= 85348:
        rn = rs["round"]
    else:
        break

print(f"Tick 85348 is in round {rn}")
end_tick = {re["round"]: re["tick"] for re in at["round_ends"]}
print(f"Round {rn} end: {end_tick[rn]} (next round start: {end_tick[rn]}... should be higher)")
print(f"Winner: {at['winner_by_round']}")

# Show kills
round_kills = [k for k in at["kills"] if k["round"] == rn]
bomb_actions = [b for b in at["bomb_actions"] if b["round"] == rn]

print(f"Kills:")
for k in sorted(round_kills, key=lambda x: x["tick"]):
    print(f"  t{k['tick']} {k['attacker']:>15s} -> {k['victim']:<15s}")
print(f"Bomb:")
for b in bomb_actions:
    print(f"  t{b['tick']} {b['type']} by {b['player']}")
if not bomb_actions:
    print("  (none)")