"""Check alive status at tick 148185 in round 15, skipping warmup."""
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

# Round 15 start tick from action_timeline.json data
ROUND15_START = 141089

deaths = parser.parse_event("player_death")

# Start with everyone alive
alive = dict.fromkeys(sid_to_name.keys(), True)
team_counts = {}
for sid, team in sid_to_team.items():
    team_counts[team] = team_counts.get(team, 0) + 1

print(f"Round 15 start: t{ROUND15_START}")
print(f"Initial: Team 2: {team_counts.get(2,0)}, Team 3: {team_counts.get(3,0)}")

# Process only round 15 deaths (after round start, before next round)
tick = 148185
last_kill_before_tick = None
for _, d in deaths.sort_values("tick").iterrows():
    dt = int(d.get("tick", 0))
    if dt < ROUND15_START:
        continue  # skip warmup / earlier rounds
    if dt > tick:
        break  # past our target tick
    victim = str(d.get("user_steamid", ""))
    if victim in alive:
        alive[victim] = False
        t = sid_to_team.get(victim, 0)
        team_counts[t] = team_counts.get(t, 0) - 1
        last_kill_before_tick = (dt, d.get("attacker_name", ""), d.get("user_name", ""))

print(f"\n=== Alive at tick {tick} (round 15 only, not counting warmup) ===")
for team in sorted(set(sid_to_team.values())):
    members = [(sid, name) for sid, name in sid_to_name.items() if sid_to_team.get(sid) == team]
    alive_members = [name for sid, name in members if alive.get(sid, False)]
    dead_members = [name for sid, name in members if not alive.get(sid, True)]
    print(f"  Team {team}: {len(alive_members)} alive -> {sorted(alive_members)}")
    if dead_members:
        print(f"           Dead: {sorted(dead_members)}")

if last_kill_before_tick:
    dt, atk, vic = last_kill_before_tick
    print(f"\nLast kill before/at t{tick}: {atk} -> {vic}")
