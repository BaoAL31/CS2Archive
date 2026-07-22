"""Generate YouTube title/description/tags for a FACEIT POV demo.

No HLTV ratings/avatars — FACEIT demos carry player names + steam IDs in the
header (via demoparser2). The POV player comes from the backlog (--player /
--steam-id); any other demo player who is a Recognised Pro
(``.data/player_accounts.json``) is treated as a "notable name" and appended
to the title.

Usage:
    python scripts/faceit/faceit_title.py <demo_path> --player <nick> [--map <map>]
                                  [--steam-id <id>] [--tournament <name>]
Prints JSON: {"title": ..., "description": ..., "tags": [...]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

MAP_DISPLAY = {
    "de_ancient": "Ancient", "de_mirage": "Mirage", "de_inferno": "Inferno",
    "de_nuke": "Nuke", "de_anubis": "Anubis", "de_overpass": "Overpass",
    "de_vertigo": "Vertigo", "de_dust2": "Dust2", "de_train": "Train",
}


def _demo_players(demo_path: Path) -> list[dict]:
    """Return list of {name, steamid, team_number} from the demo header."""
    import demoparser2 as dp
    parser = dp.DemoParser(str(demo_path))
    info = parser.parse_player_info()
    out = []
    for _, row in info.iterrows():
        out.append({
            "name": str(row.get("name", "")).strip(),
            "steamid": str(row.get("steamid", "")).strip(),
            "team_number": int(row.get("team_number", 0)),
        })
    return out


def _notable_nicks() -> set[str]:
    """Lowercase nick set for all Recognised Pros (player_accounts.json)."""
    try:
        from faceit_names import known_pros
        return known_pros()
    except Exception:
        return set()


def _map_from_demo(demo_path: Path) -> str:
    """Best-effort map name from the demo filename or header."""
    m = re.search(r"(de_[a-z0-9]+)", demo_path.name, re.I)
    if m:
        key = m.group(1).lower()
        return MAP_DISPLAY.get(key, key.replace("de_", "").capitalize())
    return "Unknown"


def build_title(player: str, map_name: str, notable: list[str], tournament: str = "") -> str:
    title = f"{player} | {map_name} | FACEIT CS2 POV"
    if notable:
        others = " & ".join(notable[:2])
        title = f"{player} vs {others} | {map_name} | FACEIT CS2 POV"
    if tournament:
        title += f" | {tournament}"
    return title[:100]


def build_description(player: str, map_name: str, notable: list[str], tournament: str,
                      demo_path: Path) -> str:
    lines = [
        f"FACEIT CS2 POV — {player} on {map_name}.",
    ]
    if notable:
        lines.append(f"Also featuring: {', '.join(notable)}.")
    if tournament:
        lines.append(f"Event: {tournament}")
    lines.append("")
    lines.append("Demo: " + demo_path.name)
    lines.append("")
    lines.append("Rendered with CS2Archive (CS2 Demo Manager + HLAE).")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path", help="Path to the FACEIT .dem file")
    ap.add_argument("--player", required=True, help="POV player nickname")
    ap.add_argument("--map", default="", help="Map name (auto-detected if omitted)")
    ap.add_argument("--steam-id", default="", help="POV steam ID (optional)")
    ap.add_argument("--tournament", default="", help="Event/tournament name")
    args = ap.parse_args()

    demo = Path(args.demo_path)
    if not demo.exists():
        print(json.dumps({"title": f"{args.player} | {args.map or 'FACEIT'}",
                          "description": "", "tags": []}))
        return

    players = _demo_players(demo)
    map_name = args.map or _map_from_demo(demo)

    # Canonicalize the POV player name (proper casing: NiKo, TeSeS, ...)
    from faceit_names import canonical_nick
    player = canonical_nick(args.player)

    notable = []
    pro_set = _notable_nicks()
    for p in players:
        nick = p["name"]
        if nick.lower() == args.player.strip().lower():
            continue
        if p["steamid"] == args.steam_id:
            continue
        if nick.lower() in pro_set:
            notable.append(canonical_nick(nick))

    title = build_title(player, map_name, notable, args.tournament)
    description = build_description(player, map_name, notable, args.tournament, demo)
    tags = ["FACEIT", "CS2", "POV", map_name, player] + notable
    tags = list(dict.fromkeys(t for t in tags if t))[:10]

    print(json.dumps({
        "title": title,
        "description": description,
        "tags": tags,
    }, indent=2))


if __name__ == "__main__":
    main()
