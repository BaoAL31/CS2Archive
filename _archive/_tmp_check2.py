"""Check all deaths near tick 148185 and verify player IDs."""
import demoparser2 as dp

demo = r"demos/hltv/2396004-liquid-vs-vitality-blast-bounty-2026-season-2/liquid-vs-vitality-m1-anubis.dem"
parser = dp.DemoParser(demo)

info = parser.parse_player_info()
sid_to_name = {}
sid_to_team = {}
for _, r in info.iterrows():
    sid = str(r.get("steamid", ""))
    if sid:
        name = str(r.get("name", ""))
        team = int(r.get("team_number", 0) or 0)
        sid_to_name[sid] = name
        sid_to_team[sid] = team

deaths = parser.parse_event("player_death")

print("All deaths in round 15 (t 141089-150327):")
count = 0
for _, d in deaths.sort_values("tick").iterrows():
    tick = int(d.get("tick", 0))
    if 141089 <= tick <= 150327:
        atk_sid = str(d.get("attacker_steamid", ""))
        vic_sid = str(d.get("user_steamid", ""))
        atk_name = str(d.get("attacker_name", ""))
        vic_name = str(d.get("user_name", ""))
        print(f"  t{tick} {atk_name:>15s} (sid={atk_sid[:8]}) -> {vic_name:<15s} (sid={vic_sid[:8]})")
        count += 1

print(f"\nTotal: {count} deaths in round 15")

# Check each player's death tick
print("\nDeath tick for each player:")
for sid, name in sorted(sid_to_name.items(), key=lambda x: sid_to_team.get(x[0], 0)):
    death_tick = None
    death_killer = None
    for _, d in deaths.iterrows():
        if str(d.get("user_steamid", "")) == sid:
            dt = int(d.get("tick", 0))
            if death_tick is None or dt < death_tick:
                death_tick = dt
                death_killer = str(d.get("attacker_name", ""))
    team = sid_to_team.get(sid, 0)
    print(f"  {name:>15s} (team {team}) -> died at t{death_tick} by {death_killer}")

# Verify the specific tick 148185
print("\nKill events at tick 148185:")
matches = deaths[deaths["tick"] == 148185]
for _, d in matches.iterrows():
    print(f"  attacker={d.get('attacker_name','')} weapon={d.get('weapon','')} victim={d.get('user_name','')}")
