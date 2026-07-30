"""Check alive status just before and at tick 148185."""
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

scenarios = [("BEFORE tick 148185 (t < 148185)", 148185, False),
             ("AT tick 148185 (t <= 148185)", 148185, True)]
for label, tick, inclusive in scenarios:
    alive = dict.fromkeys(sid_to_name.keys(), True)
    for _, d in deaths.sort_values("tick").iterrows():
        dt = int(d.get("tick", 0))
        victim = str(d.get("user_steamid", ""))
        include = dt <= tick if inclusive else dt < tick
        if include and victim in alive:
            alive[victim] = False

    print(f"\n{label}:")
    for team in sorted(set(sid_to_team.values())):
        members = [(sid, name) for sid, name in sid_to_name.items() if sid_to_team.get(sid) == team]
        alive_members = [name for sid, name in members if alive.get(sid, False)]
        dead_members = [name for sid, name in members if not alive.get(sid, True)]
        print(f"  Team {team}: {len(alive_members)} alive -> {alive_members}")
        if dead_members:
            print(f"           Dead: {dead_members}")

# Also print the exact kill at 148185
print(f"\nKill at tick 148185:")
for _, d in deaths.iterrows():
    if int(d.get("tick", 0)) == 148185:
        atk = str(d.get("attacker_name", ""))
        vic = str(d.get("user_name", ""))
        wpn = str(d.get("weapon", ""))
        print(f"  {atk} -> {vic} ({wpn})")
