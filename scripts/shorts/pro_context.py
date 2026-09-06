"""Parse LIM pro titles into fixture context + join HLTV ratings.

Title shapes:
  "frozen POV with Keystrokes (15-7) FaZe vs PARIVISION (mirage) PGL Cluj-Napoca 2026"
  "huNter- (21-14) G2 vs Gaimin Gladiators (Inferno) ESL Pro League Season 23 Stage 1"
"""
from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from hltv.update_team_demand import extract_fixture_teams, team_lookup  # noqa: E402

_LOOKUP = team_lookup()

_STAGE_RES = [
    ("final", re.compile(r"\bgrand\s*final\b|\bfinal\b", re.I)),
    ("semis", re.compile(r"\bsemi[-\s]*finals?\b", re.I)),
    ("quarters", re.compile(r"\bquarter[-\s]*finals?\b", re.I)),
    ("stage", re.compile(r"\bstage\s*\d+\b", re.I)),
    ("groups", re.compile(r"\bgroups?\b", re.I)),
    ("playoffs", re.compile(r"\bplayoffs?\b", re.I)),
]
_KD_RE = re.compile(r"\((\d+)\s*[-–]\s*(\d+)\)")
_KD_SPLIT = re.compile(r"^\s*(\d+)\s*[-–/]\s*(\d+)\s*$")
_MAP_RES = (
    ("dust2", re.compile(r"\bdust\s*2\b", re.I)),
    ("inferno", re.compile(r"\binferno\b", re.I)),
    ("mirage", re.compile(r"\bmirage\b", re.I)),
    ("nuke", re.compile(r"\bnuke\b", re.I)),
    ("anubis", re.compile(r"\banubis\b", re.I)),
    ("ancient", re.compile(r"\bancient\b", re.I)),
    ("train", re.compile(r"\btrain\b", re.I)),
    ("cache", re.compile(r"\bcache\b", re.I)),
    ("overpass", re.compile(r"\boverpass\b", re.I)),
    ("vertigo", re.compile(r"\bvertigo\b", re.I)),
)
_S_TIER = ("katowice", "cologne", "world final", "world cup", "pro league",
           "blast premier", "epicenter", "starladder")


def parse_pro_title(title: str) -> dict:
    """Teams, event text, stage bucket, title K-D, map from a pro POV title."""
    text = title or ""
    fixture = extract_fixture_teams(text, _LOOKUP)
    stage = "other"
    for name, rx in _STAGE_RES:
        if rx.search(text):
            stage = name
            break
    kd = None
    match = _KD_RE.search(text)
    if match:
        kills, deaths = int(match.group(1)), int(match.group(2))
        kd = kills / deaths if deaths else float(kills)
    game_map = ""
    for slug, rx in _MAP_RES:
        if rx.search(text):
            game_map = slug
            break
    return {"team1": fixture[0] if fixture else "",
            "team2": fixture[1] if fixture else "",
            "stage": stage, "title_kd": kd, "map": game_map,
            "text": text}


def event_tier(*texts: str) -> str:
    blob = " ".join(texts).lower()
    if "major" in blob:
        return "major"
    if any(token in blob for token in _S_TIER):
        return "s-tier"
    return "regular"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def normalize_stage(text: str | None) -> str:
    """Fold any stage string (title or ratings file) into buckets."""
    raw = (text or "").strip().lower()
    if not raw or raw == "other":
        return "other"
    for name, rx in _STAGE_RES:
        if rx.search(raw):
            return name
    return "other"
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_kd_ratio(kd) -> float | None:
    """'39-45' -> 0.87. None when unparseable."""
    if isinstance(kd, (int, float)):
        return float(kd)
    match = _KD_SPLIT.match(str(kd or "").strip())
    if not match:
        return None
    kills, deaths = float(match.group(1)), float(match.group(2))
    return kills if deaths == 0 else kills / deaths


def kd_bucket(kd: float | None) -> str:
    if kd is None:
        return "unknown"
    if kd >= 1.5:
        return "1.5+"
    if kd >= 1.0:
        return "1.0+"
    return "sub1.0"


def index_ratings(root: Path | None = None) -> list[dict]:
    """Ratings files keyed by team-pair + per-map player ratings + stage."""
    base = root or (ROOT / "demos" / "analysis")
    out: list[dict] = []
    for path in glob.glob(str(base / "*ratings*.json")):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tables = payload.get("tables") or []
        by_map: dict[str, dict[str, dict]] = {}
        for table in tables:
            game_map = str(table.get("map") or "").strip().lower()
            if not game_map or game_map == "series overall":
                continue
            for player in table.get("players") or []:
                nick = str(player.get("nickname") or "").strip().lower()
                try:
                    rating = float(player.get("rating") or 0)
                except (TypeError, ValueError):
                    rating = 0.0
                kd = parse_kd_ratio(player.get("kd"))
                if nick and (rating or kd is not None):
                    by_map.setdefault(game_map, {})[nick] = {
                        "rating": rating, "kd": kd}
        teams = {_norm(t.get("team")) for t in tables if t.get("team")}
        url = str(payload.get("url") or "")
        hit = re.search(r"/matches/(\d+)/", url)
        out.append({"path": path, "teams": teams,
                    "match_id": hit.group(1) if hit else "",
                    "stage": str(payload.get("match_stage") or ""),
                    "n_maps": len(by_map),
                    "by_map": by_map})
    return out


def lookup_rating(index: list[dict], team1: str, team2: str,
                  game_map: str, nick: str) -> tuple[dict, str, dict]:
    """(stats, stage, entry) for this POV; empty stats when unresolvable."""
    want = {_norm(team1), _norm(team2)} - {""}
    if len(want) < 2:
        return {}, "", {}
    for entry in index:
        if not want <= entry["teams"]:
            continue
        table = entry["by_map"].get((game_map or "").lower(), {})
        stats = table.get((nick or "").lower(), {})
        if not stats:
            for alias, value in table.items():
                if _norm(alias) == _norm(nick):
                    stats = value
                    break
        if stats:
            return stats, entry["stage"], entry
    return {}, "", {}


def rating_bucket(rating: float | None) -> str:
    if not rating:
        return "unknown"
    if rating >= 1.5:
        return "1.5+"
    if rating >= 1.2:
        return "1.2+"
    if rating >= 1.0:
        return "1.0+"
    return "sub1.0"


def load_match_scores(path: Path | None = None) -> dict:
    target = path or (ROOT / ".data" / "match_scores.json")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def series_context(entry: dict, scores: dict, org: str | None) -> dict:
    """Decider / won / OT for this POV from cached match scores."""
    out = {"decider": "unknown", "won": "unknown", "ot": "unknown"}
    info = scores.get(entry.get("match_id") or "", {}) if entry else {}
    if not info:
        if entry and entry.get("n_maps", 0) >= 3:
            out["decider"] = "yes"
        elif entry:
            out["decider"] = "no"
        return out
    out["decider"] = "yes" if info.get("decider") else "no"
    out["ot"] = "yes" if info.get("ot") else "no"
    side = info.get("winner_side") or ""
    if side and org:
        sides = {}
        for entry_map in info.get("maps") or []:
            sides.setdefault("left", entry_map.get("left_team", ""))
            sides.setdefault("right", entry_map.get("right_team", ""))
        winner = sides.get(side, "")
        if winner:
            out["won"] = ("yes" if _norm(winner) == _norm(org) else "no")
    return out


def derby_heat(team1: str, team2: str, fixtures: list[dict]) -> float | None:
    """Best historic highlight views for this exact fixture pair."""
    from hltv.update_team_demand import resolve_team_name
    left, right = resolve_team_name(team1), resolve_team_name(team2)
    if not left or not right or left == right:
        return None
    best = 0
    for fixture in fixtures or []:
        if {fixture.get("team1"), fixture.get("team2")} != {left, right}:
            continue
        try:
            best = max(best, int(fixture.get("views") or 0))
        except (TypeError, ValueError):
            continue
    return float(best) if best > 0 else None
    if rating is None:
        return "unknown"
    if rating >= 1.5:
        return "1.5+"
    if rating >= 1.2:
        return "1.2+"
    if rating >= 1.0:
        return "1.0+"
    return "sub1.0"
