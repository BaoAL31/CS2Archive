from __future__ import annotations

import json
import re
from pathlib import Path

ANALYSIS_DIR = Path("demos/analysis")
AVATAR_DIR = Path("demos/avatars")
YOUTUBE_DIR = Path("youtube")

# HLTV human-readable map name → CS2 game file name
MAP_NAME_MAP: dict[str, str] = {
    "Mirage": "de_mirage",
    "Inferno": "de_inferno",
    "Dust2": "de_dust2",
    "Nuke": "de_nuke",
    "Ancient": "de_ancient",
    "Anubis": "de_anubis",
    "Vertigo": "de_vertigo",
    "Overpass": "de_overpass",
    "Cache": "de_cache",
    "Train": "de_train",
    "Office": "cs_office",
    "Italy": "cs_italy",
}

# Reverse map: game file name → HLTV name
GAME_MAP_MAP: dict[str, str] = {v: k for k, v in MAP_NAME_MAP.items()}


def slugify(team1: str, team2: str) -> str:
    t1 = re.sub(r"[^a-z0-9]", "", team1.lower())
    t2 = re.sub(r"[^a-z0-9]", "", team2.lower())
    return f"{t1}-vs-{t2}"


def find_ratings_file(match_slug: str) -> Path | None:
    path = ANALYSIS_DIR / f"{match_slug}_ratings.json"
    if path.exists():
        return path
    for f in ANALYSIS_DIR.glob(f"{match_slug}*.json"):
        return f
    return None


def load_ratings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_player_stats(
    ratings: dict, player_nickname: str, map_name: str
) -> dict | None:
    for table in ratings.get("tables", []):
        if table["map"].lower() != map_name.lower():
            continue
        for p in table["players"]:
            if p["nickname"].strip().lower() == player_nickname.strip().lower():
                return p
    return None


def get_team_names(ratings: dict, map_name: str) -> tuple[str, str]:
    teams: list[str] = []
    for table in ratings.get("tables", []):
        if table["map"].lower() == map_name.lower():
            teams.append(table["team"].strip())
    if len(teams) >= 2:
        return (teams[0], teams[1])
    for table in ratings.get("tables", []):
        if table["map"] == "Series Overall":
            teams.append(table["team"].strip())
    if len(teams) >= 2:
        return (teams[0], teams[1])
    return ("Team 1", "Team 2")


def get_avatar_path(nickname: str) -> Path | None:
    name = nickname.strip().lower()
    png = AVATAR_DIR / f"{name}.png"
    if png.exists():
        return png
    for ext in (".jpg", ".jpeg", ".webp"):
        p = AVATAR_DIR / f"{name}{ext}"
        if p.exists():
            return p
    return None


def get_slug_from_url(url: str) -> str | None:
    m = re.search(r"/matches/\d+/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None
