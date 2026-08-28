"""HLTV team-world-ranking based star weighting for notable FACEIT matches.

The daily notable picker weights matches by how many *star* pros (players on
top-HLTV-ranked teams) they contain, instead of raw FACEIT ELO.

Two pieces of data:
  1. HLTV team world ranking  ->  {team_name: rank}   (fetched, cached 24h)
  2. Curated pro -> HLTV team  ->  PRO_TEAM            (this file)

Why curated and not scraped per-player: HLTV player pages render the "Teams"
history via a JS carousel (absent from static HTML) and the static "Current
team" field is frequently "No team" for FACEIT pros who are between rosters or
on mixed lobbies. A curated map is deterministic and fast. Extend PRO_TEAM as
needed; pros absent from it contribute 0 star bonus (intentional).

Team names in PRO_TEAM MUST match the exact strings HLTV uses in its ranking
table (see RANKING_TEAM_NAMES for the current top 30).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Allow running both as a script and an imported module.
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "scripts"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scrapers.hltv import HLTVScraper  # noqa: E402

RANKING_URL = "https://www.hltv.org/ranking/teams/"
CACHE_PATH = _ROOT / ".data" / "hltv_team_ranking.json"
CACHE_TTL = 24 * 3600  # seconds

# Current HLTV top-30 team names (kept for reference / validation).
RANKING_TEAM_NAMES = [
    "Spirit", "Falcons", "FURIA", "Vitality", "MOUZ", "FUT", "Legacy",
    "Natus Vincere", "G2", "FaZe", "Aurora", "9z", "Astralis", "The MongolZ",
    "BetBoom", "PARIVISION", "B8", "GamerLegion", "magic", "paiN", "BIG",
    "Liquid", "3DMAX", "Ninjas in Pyjamas", "MIBR", "TYLOO", "Alliance",
    "JiJieHao", "HOTU", "HEROIC",
]

# Repo's authoritative player->team map, derived from HLTV demos.
ROSTER_PATH = _ROOT / ".data" / "team_roster.json"

# Roster team names -> HLTV ranking team names (ranking uses canonical spelling).
TEAM_ALIASES = {
    "NAVI": "Natus Vincere",
    "NaVi": "Natus Vincere",
    "Furia": "FURIA",
    "NAVI Junior": "Natus Vincere",
}

# Manual overrides (canonical nick -> HLTV ranking team). Wins over the roster;
# only set when the roster is stale. Values must match HLTV ranking spelling.
PRO_TEAM_OVERRIDE: dict[str, str] = {}

_ROSTER_CACHE: dict | None = None


def _load_roster() -> dict:
    """nickname (lowercase) -> current_team (alias-normalized)."""
    global _ROSTER_CACHE
    if _ROSTER_CACHE is not None:
        return _ROSTER_CACHE
    out: dict[str, str] = {}
    if ROSTER_PATH.exists():
        try:
            data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
            for info in data.get("players", {}).values():
                n = (info.get("nickname") or "").strip().lower()
                t = info.get("current_team")
                if n and t:
                    out[n] = TEAM_ALIASES.get(t, t)
        except Exception:
            pass
    _ROSTER_CACHE = out
    return out

# Star bonus by HLTV rank tier.
def rank_bonus(rank: int | None) -> int:
    if not rank:
        return 0
    if rank <= 5:
        return 400_000
    if rank <= 10:
        return 250_000
    if rank <= 20:
        return 120_000
    if rank <= 30:
        return 60_000
    return 0


def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - data.get("fetched_at", 0) > CACHE_TTL:
            return None
        return data.get("teams", {})
    except Exception:
        return None


def _save_cache(teams: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"fetched_at": int(time.time()), "teams": teams}, indent=2),
        encoding="utf-8",
    )


async def fetch_team_ranking(force: bool = False) -> dict[str, int]:
    """Return {team_name: rank(1-based)} from HLTV's world ranking, cached 24h."""
    if not force:
        cached = _load_cache()
        if cached:
            return cached
    s = HLTVScraper()
    try:
        html = await s._get_page_content(RANKING_URL)
    finally:
        await s.close()
    import re
    teams: dict[str, int] = {}
    for blk in re.finditer(
        r'<div class="ranked-team standard-box">(.*?)</div></div>\s*</div>\s*</div>',
        html, re.S,
    ):
        pos = re.search(r'position[^\"]*">#(\d+)<', blk.group(1))
        name = re.search(r'alt="([^"]+)"', blk.group(1))
        if pos and name:
            teams[name.group(1)] = int(pos.group(1))
    # Defensive: only keep the first 30 (one page).
    teams = dict(sorted(teams.items(), key=lambda kv: kv[1])[:30])
    _save_cache(teams)
    return teams


def pro_team_rank(nick: str, ranking: dict[str, int] | None = None) -> int | None:
    """Return HLTV world rank (1-based) of the team `nick` plays for, or None.

    Team comes from the repo's HLTV roster (.data/team_roster.json), with
    PRO_TEAM_OVERRIDE as a manual fallback (e.g. stale roster).
    """
    team = PRO_TEAM_OVERRIDE.get(nick) or PRO_TEAM_OVERRIDE.get(nick.lower()) \
        or _load_roster().get(nick.lower())
    if not team:
        return None
    if ranking is None:
        ranking = _load_cache() or {}
    return ranking.get(team)


def star_bonus_for_pros(pros: list[str], ranking: dict[str, int] | None = None) -> int:
    """Sum of rank bonuses across the given pros (canonical nicks)."""
    if ranking is None:
        ranking = _load_cache() or {}
    return sum(rank_bonus(pro_team_rank(p, ranking)) for p in pros)


if __name__ == "__main__":
    import asyncio

    async def _main():
        r = await fetch_team_ranking(force=True)
        print("Ranked teams:", len(r))
        for t, rk in sorted(r.items(), key=lambda kv: kv[1]):
            print(f"  {rk:>2}  {t}")
        # sanity: show bonuses for a few stars
        for n in ("m0NESY", "b1t", "molodoy", "HeavyGod", "xeedo"):
            rk = pro_team_rank(n, r)
            print(f"  {n:10} team_rank={rk} bonus={rank_bonus(rk)}")

    asyncio.run(_main())
