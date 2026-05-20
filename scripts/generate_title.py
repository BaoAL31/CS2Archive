"""Generate YouTube video title and description from ratings data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def extract_team_names(ratings_data: dict) -> tuple[str, str]:
    tables = ratings_data.get("tables", [])
    teams: list[str] = []
    for t in tables:
        name = t.get("team", "").strip()
        if name and name not in teams:
            teams.append(name)
    if len(teams) >= 2:
        return (teams[0], teams[1])
    return ("Team A", "Team B")


def find_player_map_stats(ratings_data: dict, player: str, map_name: str) -> dict | None:
    tables = ratings_data.get("tables", [])
    player_lower = player.strip().lower()

    for t in tables:
        t_map = t.get("map", "").strip().lower()
        if "series overall" in t_map:
            continue
        if map_name.lower() not in t_map:
            continue
        for p in t.get("players", []):
            nick = p.get("nickname", "").strip().lower()
            if nick == player_lower:
                return p
    return None


def shorten_team(name: str) -> str:
    name = name.strip()
    overrides = {
        "natus vincere": "NAVI",
        "ninjas in pyjamas": "NiP",
        "team vitality": "Vitality",
        "team spirit": "Spirit",
        "team liquid": "Liquid",
        "faze clan": "FaZe",
        "gamerlegion": "GL",
        "betboom": "BB",
        "the mongolz": "MongolZ",
        "bc.game": "BC.Game",
        "passion ua": "Passion UA",
        "fnatic": "Fnatic",
        "paiN": "paiN",
        "complexity": "COL",
    }
    key = name.lower()
    if key in overrides:
        return overrides[key]
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate YouTube video title and description")
    parser.add_argument("ratings_json", help="Path to ratings JSON file")
    parser.add_argument("--player", required=True, help="Player nickname")
    parser.add_argument("--map", required=True, help="Map name")
    parser.add_argument("--tournament", default="", help="Tournament name")
    parser.add_argument("--team-a", help="Override team A name")
    parser.add_argument("--team-b", help="Override team B name")
    args = parser.parse_args()

    path = Path(args.ratings_json)
    if not path.exists():
        print(json.dumps({"error": f"Ratings file not found: {path}"}))
        sys.exit(1)

    data = json.loads(path.read_text())

    team_a, team_b = extract_team_names(data)
    if args.team_a:
        team_a = args.team_a
    if args.team_b:
        team_b = args.team_b

    team_a_short = shorten_team(team_a)
    team_b_short = shorten_team(team_b)

    stats = find_player_map_stats(data, args.player, args.map)
    rating = stats.get("rating", "").strip() if stats else "?.??"
    kd = stats.get("kd", "").strip() if stats else ""
    adr = stats.get("adr", "").strip() if stats else ""
    kast = stats.get("kast", "").strip() if stats else ""

    tournament = args.tournament or ""
    if not tournament:
        m = re.search(r"([A-Za-z\s]+20\d{2})", path.stem.replace("_", " "))
        if m:
            tournament = m.group(1)

    stage = data.get("match_stage", "") if isinstance(data, dict) else ""

    title = " | ".join(filter(None, [
        args.player,
        f"{rating} Rating" if rating != "?.??" else "",
        f"{team_a_short} vs {team_b_short}",
        args.map,
        stage or None,
        tournament or None,
    ]))

    desc_lines = [
        f"{args.player}'s POV on {args.map}",
    ]
    if stage:
        desc_lines.append(f"{tournament} — {stage}" if tournament else stage)
    elif tournament:
        desc_lines.append(tournament)
    if team_a and team_b:
        desc_lines.append(f"{team_a} vs {team_b}")
    desc_lines.append("")
    desc_lines.append(f"HLTV Rating 3.0: {rating}")
    if kd:
        desc_lines.append(f"K-D: {kd}")
    if adr:
        desc_lines.append(f"ADR: {adr}")
    if kast:
        desc_lines.append(f"KAST: {kast}")
    desc_lines.append("")
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(path))
    if date_match:
        desc_lines.append(date_match.group(1))

    description = "\n".join(desc_lines)

    # Match format from stage
    stage_tag = None
    if stage:
        stage_lower = stage.lower()
        if "final" in stage_lower:
            stage_tag = "Grand Final"
        elif "semi" in stage_lower:
            stage_tag = "Playoffs Semi-Final"

    tags = list(filter(None, [
        args.player,
        args.map,
        team_a_short,
        team_b_short,
        tournament or None,
        stage_tag,
        f"{args.player} cs2",
        f"{team_a_short} CS2",
        f"{team_b_short} CS2",
        "CS2",
        "Counter-Strike 2",
        "Counter Strike 2",
        "CS2 POV",
        "CS2 gameplay",
        "CS2 full match",
        "POV",
        "Full Match",
        "HLTV",
        "Counter-Strike",
        "eSports",
        "Competitive",
        "Gaming",
    ]))

    print(json.dumps({"title": title, "description": description, "tags": tags}))


if __name__ == "__main__":
    main()
