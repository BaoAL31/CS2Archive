"""Check round 10: mezii clutch claim."""
import json
at = json.load(open("renders/hl-liquid-vs-vitality-m1-anubis/action_timeline.json"))

round10_kills = [k for k in at["kills"] if k["round"] == 10]
round10_bombs = [b for b in at["bomb_actions"] if b["round"] == 10]

print("=== Round 10 kills ===")
for k in sorted(round10_kills, key=lambda x: x["tick"]):
    print(f"  t{k['tick']} {k['attacker']:>15s} -> {k['victim']:<15s}")

print()
print("=== Round 10 bomb events ===")
for b in sorted(round10_bombs, key=lambda x: x["tick"]):
    print(f"  t{b['tick']} {b['type']} by {b['player']}")
if not round10_bombs:
    print("  (none)")

# Short says start_tick=99615, end_tick=100462, win_event=team_win
# Which team actually won?
# Round 10 end tick = 100462. At this tick, the last kill determines the winner.
# Who is the last killer?
last_kill = round10_kills[-1] if round10_kills else None
if last_kill:
    print(f"\nLast kill: {last_kill['attacker']} -> {last_kill['victim']} at t{last_kill['tick']}")
    # The killer's team survives
    import demoparser2 as dp
    parser = dp.DemoParser(r"demos/hltv/2396004-liquid-vs-vitality-blast-bounty-2026-season-2/liquid-vs-vitality-m1-anubis.dem")
    info = parser.parse_player_info()
    for _, r in info.iterrows():
        sid = str(r.get("steamid", ""))
        if sid == last_kill["attacker_steam_id"]:
            team = int(r.get("team_number", 0))
            name = r.get("name", "")
            print(f"  {name} is on team {team}")
        if sid == last_kill["victim_steam_id"]:
            team = int(r.get("team_number", 0))
            name = r.get("name", "")
            print(f"  {name} is on team {team}")