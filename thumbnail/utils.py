from __future__ import annotations

import json
import re
from pathlib import Path

from PIL import Image

ANALYSIS_DIR = Path("demos/analysis")
AVATAR_DIR = Path("demos/avatars")
YOUTUBE_DIR = Path("youtube")

# Avatar subfolder layout: demos/avatars/{nick}/{source}/{nick}.png
# where source is one of "hltv" or "faceit". HLTV takes priority.
AVATAR_SOURCES = ("hltv", "faceit")
AVATAR_EXTS = (".png", ".jpg", ".jpeg", ".webp")

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
    ABBR = {
        "Natus Vincere": "NaVi",
        "GamerLegion": "GL",
        "Team Falcons": "Falcons",
        "Natus Vincere Junior": "NAVI Junior",
    }
    raw_teams: list[str] = []
    for table in ratings.get("tables", []):
        if table["map"].lower() == map_name.lower():
            raw_teams.append(table["team"].strip())
    if len(raw_teams) >= 2:
        t1, t2 = raw_teams[0], raw_teams[1]
        return (ABBR.get(t1, t1), ABBR.get(t2, t2))
    for table in ratings.get("tables", []):
        if table["map"] == "Series Overall":
            raw_teams.append(table["team"].strip())
    if len(raw_teams) >= 2:
        t1, t2 = raw_teams[0], raw_teams[1]
        return (ABBR.get(t1, t1), ABBR.get(t2, t2))
    return ("Team 1", "Team 2")


def get_avatar_path(nickname: str) -> Path | None:
    """Return best avatar path for a nickname.

    Resolution order (per player folder):
      1. demos/avatars/{nick}/hltv/{nick}[_N].<ext>   (HLTV pro bodyshot wins)
      2. demos/avatars/{nick}/faceit/{nick}[_N].<ext>

    Within a source folder, the largest PNG (by pixel area) is chosen so the
    best-centered / highest-res variant wins.
    """
    name = nickname.strip().lower()

    def _best_in(folder: Path) -> Path | None:
        if not folder.is_dir():
            return None
        cands: list[Path] = []
        for ext in AVATAR_EXTS:
            cands.extend(folder.glob(f"{name}*.{ext.lstrip('.')}"))
        if not cands:
            return None
        best = max(
            cands,
            key=lambda p: (Image.open(p).size[0] * Image.open(p).size[1])
            if _safe_size(p)
            else 0,
        )
        return best

    for source in AVATAR_SOURCES:
        p = _best_in(AVATAR_DIR / name / source)
        if p:
            return p
    return None


def _safe_size(p: Path) -> bool:
    try:
        Image.open(p).size
        return True
    except Exception:
        return False


def get_slug_from_url(url: str) -> str | None:
    m = re.search(r"/matches/\d+/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None
