"""Score HLTV backlog cards with team/individual stars + map rating.

Chips (same 250k-scale as FACEIT notable, minus lobby ELO):

  match_team      both teams' highlight-channel demand, summed (cap 400k)
  match_highlight this fixture's recent highlight views (cap 200k)
  star            POV player's org rank / 2, K/D >= 1 (cap 200k)
  demand          max(POV-channel index, highlight-named player index) (cap 200k)
  rating          HLTV Rating 3.0 above 1.00 (cap 160k)

Usage:
    python scripts/hltv/score_cards.py backlog/<match_slug>
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from hltv.update_team_demand import (  # noqa: E402
    load_team_demand,
    maybe_refresh as maybe_refresh_team_demand,
    resolve_team_name,
)
from hltv_ranking import pro_team_rank, rank_bonus  # noqa: E402
from scrape_notable import load_player_demand_index, star_bonus  # noqa: E402

SCORE_VERSION = 1
DEMAND_SCALE = 250_000
HIGHLIGHT_VIEW_FLOOR = 1_000
HIGHLIGHT_LOOKBACK_DAYS = 7
PLAYER_DEMAND_STALE_DAYS = 7

_KD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[–\-/]\s*(\d+(?:\.\d+)?)")


def parse_kd_ratio(kd) -> float:
    """K/D string from HLTV ratings (`20-10`, `20–10`) or a numeric ratio."""
    if isinstance(kd, (int, float)):
        return float(kd)
    text = str(kd or "").strip()
    if not text:
        return 0.0
    match = _KD_RE.search(text)
    if match:
        kills = float(match.group(1))
        deaths = float(match.group(2))
        return kills if deaths == 0 else kills / deaths
    try:
        return float(text)
    except ValueError:
        return 0.0


def demand_points(index: float) -> int:
    return round(max(0.0, float(index) - 1.0) * DEMAND_SCALE)


def rating_bonus(rating: float) -> int:
    """HLTV Rating 3.0: 1.00 → 0, 2.00 → 80k, 3.00 → 160k cap."""
    return round(min(max(float(rating) - 1.0, 0.0) * 80_000, 160_000))


def _team_index_value(name: str, index: dict[str, float]) -> float:
    if not name or not index:
        return 1.0
    canon = resolve_team_name(name) or name
    if canon in index:
        return float(index[canon])
    lowered = {str(key).casefold(): float(value) for key, value in index.items()}
    return lowered.get(canon.casefold(), lowered.get(str(name).casefold(), 1.0))


def team_demand_points(name: str, index: dict[str, float]) -> int:
    return demand_points(_team_index_value(name, index))


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def match_highlight_bonus(
    team1: str,
    team2: str,
    fixtures: list[dict],
    *,
    now: datetime | None = None,
) -> int:
    """Log-scaled views of a recent highlight for this exact fixture.

    1k views → 0, 10k → ~67k, 100k → ~133k, 1M → 200k cap.
    """
    left = resolve_team_name(team1)
    right = resolve_team_name(team2)
    if not left or not right or left == right:
        return 0
    pair = {left, right}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=HIGHLIGHT_LOOKBACK_DAYS)
    best = 0
    for fixture in fixtures or []:
        fpair = {fixture.get("team1"), fixture.get("team2")}
        if fpair != pair:
            continue
        published = _parse_published(fixture.get("published_at"))
        if published is not None and published < cutoff:
            continue
        try:
            views = int(fixture.get("views") or 0)
        except (TypeError, ValueError):
            views = 0
        if views > best:
            best = views
    if best < HIGHLIGHT_VIEW_FLOOR:
        return 0
    return round(min(200_000, max(0.0, (math.log10(best) - 3.0) / 3.0) * 200_000))


def _org_rank(nick: str, team: str, ranking: dict[str, int] | None) -> int | None:
    ranking = ranking or {}
    canon = resolve_team_name(team)
    if canon:
        if canon in ranking:
            return ranking[canon]
        lowered = {key.casefold(): value for key, value in ranking.items()}
        if canon.casefold() in lowered:
            return lowered[canon.casefold()]
    if team:
        lowered = {key.casefold(): value for key, value in ranking.items()}
        if team.casefold() in lowered:
            return lowered[team.casefold()]
    return pro_team_rank(nick, ranking)


def player_demand_index(
    nick: str,
    player_demand: dict[str, float],
    highlight_players: dict[str, float],
) -> float:
    key = nick.casefold()
    pov = float(player_demand.get(key, 1.0)) if player_demand else 1.0
    highlight = float(highlight_players.get(key, 1.0)) if highlight_players else 1.0
    return max(pov, highlight, 1.0)


def score_card(
    meta: dict,
    *,
    ranking: dict[str, int] | None = None,
    player_demand: dict[str, float] | None = None,
    team_demand: dict[str, float] | None = None,
    highlight_players: dict[str, float] | None = None,
    fixtures: list[dict] | None = None,
    fixture_teams: tuple[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Return explainable chips + weight for one backlog card."""
    nick = str(meta.get("player") or "")
    team = str(meta.get("team") or "")
    opponent = str(meta.get("opponent") or "")
    if fixture_teams:
        if not resolve_team_name(team):
            team = fixture_teams[0]
        if not resolve_team_name(opponent):
            t1, t2 = fixture_teams
            player_team = resolve_team_name(team)
            if player_team and resolve_team_name(t1) == player_team:
                opponent = t2
            elif player_team and resolve_team_name(t2) == player_team:
                opponent = t1
            else:
                opponent = t2
    match_left, match_right = team, opponent

    try:
        rating = float(meta.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    kd = parse_kd_ratio(meta.get("kd"))
    ranking = ranking or {}
    player_demand = player_demand or {}
    team_demand = team_demand or {}
    highlight_players = highlight_players or {}

    raw_star = rank_bonus(_org_rank(nick, team, ranking))
    star = star_bonus(raw_star, kd=kd)
    demand_index = player_demand_index(nick, player_demand, highlight_players)
    demand = demand_points(demand_index)
    match_team = team_demand_points(match_left, team_demand) + team_demand_points(
        match_right, team_demand
    )
    highlight = match_highlight_bonus(
        match_left, match_right, fixtures or [], now=now
    )
    rating_pts = rating_bonus(rating)
    weight = star + demand + match_team + highlight + rating_pts
    return {
        "score_version": SCORE_VERSION,
        "raw_star_bonus": raw_star,
        "star_bonus": star,
        "market_demand_bonus": demand,
        "match_team_bonus": match_team,
        "match_highlight_bonus": highlight,
        "rating_bonus": rating_pts,
        "demand_index": demand_index,
        "kd_ratio": round(kd, 2),
        "weight": weight,
    }


def load_indexes() -> dict:
    team = load_team_demand()
    return {
        "ranking": _load_ranking_cache(),
        "player_demand": load_player_demand_index(),
        "team_demand": team.get("index") or {},
        "highlight_players": team.get("players") or {},
        "fixtures": team.get("fixtures") or [],
    }


def _load_ranking_cache() -> dict[str, int]:
    from hltv_ranking import CACHE_PATH, _load_cache

    cached = _load_cache()
    if cached:
        return cached
    if not CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload.get("teams") or {}


def attach_scores(
    cards: list[tuple[str, dict]],
    *,
    indexes: dict | None = None,
    fixture_teams: tuple[str, str] | None = None,
) -> list[tuple[str, dict]]:
    indexes = indexes or load_indexes()
    out: list[tuple[str, dict]] = []
    for path, meta in cards:
        scored = {
            **meta,
            **score_card(
                meta,
                ranking=indexes.get("ranking"),
                player_demand=indexes.get("player_demand"),
                team_demand=indexes.get("team_demand"),
                highlight_players=indexes.get("highlight_players"),
                fixtures=indexes.get("fixtures"),
                fixture_teams=fixture_teams,
            ),
        }
        out.append((path, scored))
    return out


def maybe_refresh_indexes(*, scrape: bool = True) -> None:
    """Refresh highlight team stars (24h) and POV player stars (7d) if stale."""
    maybe_refresh_team_demand(max_age_hours=24, scrape=scrape)
    demand_path = ROOT / ".data" / "player_demand_index.json"
    stale = True
    if demand_path.exists():
        try:
            payload = json.loads(demand_path.read_text(encoding="utf-8"))
            stamp = _parse_published(payload.get("updated_at"))
            if stamp is not None:
                stale = datetime.now(timezone.utc) - stamp >= timedelta(
                    days=PLAYER_DEMAND_STALE_DAYS
                )
        except (OSError, json.JSONDecodeError):
            stale = True
    if not stale:
        return
    try:
        from update_player_demand import refresh as refresh_player_demand

        refresh_player_demand(scrape=scrape)
    except Exception as exc:
        print(f"[demand] POV scrape failed: {type(exc).__name__}: {exc}",
              flush=True)


def format_score(meta: dict) -> str:
    return (
        f"star={meta.get('star_bonus', 0)} demand={meta.get('market_demand_bonus', 0)} "
        f"teams={meta.get('match_team_bonus', 0)} rating={meta.get('rating_bonus', 0)} "
        f"highlight={meta.get('match_highlight_bonus', 0)} total={meta.get('weight', 0)}"
    )


def _iter_backlog_cards(root: Path) -> list[tuple[str, dict]]:
    cards: list[tuple[str, dict]] = []
    for bucket in ("high", "medium", "low"):
        folder = root / bucket
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                rel = str(path.resolve().relative_to(ROOT)).replace("\\", "/")
            except ValueError:
                rel = str(path).replace("\\", "/")
            cards.append((rel, meta))
    return cards


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backlog", type=Path, help="backlog/<match_slug> folder")
    args = parser.parse_args()
    folder = args.backlog
    if not folder.is_absolute():
        folder = ROOT / folder
    cards = attach_scores(_iter_backlog_cards(folder))
    cards.sort(key=lambda item: (-item[1].get("weight", 0), item[0]))
    print(f"{len(cards)} card(s) in {folder}")
    for path, meta in cards:
        print(
            f"  {meta.get('player')} {meta.get('map')} "
            f"r={meta.get('rating')} {format_score(meta)}"
        )
        print(f"    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
