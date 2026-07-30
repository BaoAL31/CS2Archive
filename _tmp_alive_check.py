"""Check alive status at a specific tick by replaying death events."""
import demoparser2 as dp

demo = r"demos/hltv/2396004-liquid-vs-vitality-blast-bounty-2026-season-2/liquid-vs-vitality-m1-anubis.dem"
parser = dp.DemoParser(demo)

info = parser.parse_player_info()
sid_to_name = {}
sid_to_team = {}
for _, r in info.iterrows():
    sid = str(r.get("steamid", ""))
    if sid:
        sid_to_name[sid] = str(r.get("name", ""))
        sid_to_team[sid] = int(r.get("team_number", 0) or 0)

tick = 148185

# Walk deaths in order; everyone alive until killed
deaths = parser.parse_event("player_death")
alive = dict.fromkeys(sid_to_name.keys(), True)
alive_count = {2: 5, 3: 5}

for _, d in deaths.sort_values("tick").iterrows():
    dt = int(d.get("tick", 0))
    if dt > tick:
        break
    victim = str(d.get("user_steamid", ""))
    if victim in alive and alive[victim]:
        alive[victim] = False
        t = sid_to_team.get(victim, 0)
        if t in alive_count:
            alive_count[t] -= 1

print(f"=== Alive at tick {tick} (by replaying deaths) ===")
for team in (2, 3):
    team_name = "Liquid" if team == 2 else "Vitality"
    team_players = [sid for sid, t in sid_to_team.items() if t == team and alive.get(sid, False)]
    print(f"\nTeam {team} ({team_name}): {len(team_players)} alive")
    for sid in sorted(team_players, key=lambda s: sid_to_name.get(s, s)):
        print(f"  {sid_to_name.get(sid, sid)} ({sid})")

dead = [sid for sid, a in alive.items() if not a]
print(f"\nDead ({len(dead)}): {[sid_to_name.get(s, s[:8]) for s in dead]}")
