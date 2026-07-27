"""
CS2Archive — HLTV Player Resolver (pure logic, no network/Playwright)

Resolves HLTV profile URLs/IDs from saved accounts and ratings JSON.
"""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from PIL import Image

MIN_AVATAR_RES = 300

_HLTV_PLAYER_ID_RE = re.compile(r"/player/(\d+)/", re.IGNORECASE)


class HltvPlayerResolution(TypedDict):
    player_url: str
    player_id: str
    source: str


class SearchPlayerCandidate(TypedDict):
    player_url: str
    player_id: str
    nickname: str


def normalize_pipeline_player_key(player_key: str) -> str:
    """Normalize pipeline --player key for case-insensitive lookups."""
    return player_key.strip().lower()


def hltv_player_id_from_url(url: str | None) -> str | None:
    """Extract numeric HLTV player ID from a profile URL."""
    if not url:
        return None
    m = _HLTV_PLAYER_ID_RE.search(url)
    return m.group(1) if m else None


def _make_resolution(player_url: str, source: str, player_id: str = "") -> HltvPlayerResolution | None:
    url = (player_url or "").strip()
    pid = (player_id or "").strip() or (hltv_player_id_from_url(url) or "")
    if not url and not pid:
        return None
    return {
        "player_url": url,
        "player_id": pid,
        "source": source,
    }


def _account_fields(account: Any) -> tuple[str, str, str]:
    if isinstance(account, dict):
        return (
            str(account.get("nickname", "")),
            str(account.get("hltv_player_id", "") or ""),
            str(account.get("hltv_player_url", "") or ""),
        )
    return (
        str(getattr(account, "nickname", "")),
        str(getattr(account, "hltv_player_id", "") or ""),
        str(getattr(account, "hltv_player_url", "") or ""),
    )


def find_account_by_player_key(
    accounts: list[Any],
    player_key: str,
) -> Any | None:
    """Find a saved account whose nickname matches the pipeline player key."""
    key = normalize_pipeline_player_key(player_key)
    for account in accounts:
        nick, _, _ = _account_fields(account)
        if normalize_pipeline_player_key(nick) == key:
            return account
    return None


def resolve_from_account(account: Any) -> HltvPlayerResolution | None:
    """Build resolution from a single account record when HLTV fields are set."""
    _, player_id, player_url = _account_fields(account)
    return _make_resolution(player_url, "account", player_id)


def resolve_from_accounts(
    accounts: list[Any],
    player_key: str,
) -> HltvPlayerResolution | None:
    """Lookup HLTV profile from saved player accounts by pipeline player key."""
    account = find_account_by_player_key(accounts, player_key)
    if account is None:
        return None
    return resolve_from_account(account)


def roster_nicknames_from_ratings(ratings: dict) -> list[str]:
    """Return deduplicated roster nicknames (lowercased) from ratings tables."""
    seen: set[str] = set()
    ordered: list[str] = []
    for table in ratings.get("tables", []):
        for player in table.get("players", []):
            nick = normalize_pipeline_player_key(str(player.get("nickname", "")))
            if nick and nick not in seen:
                seen.add(nick)
                ordered.append(nick)
    return ordered


def resolve_from_ratings(
    ratings: dict,
    player_key: str,
) -> HltvPlayerResolution | None:
    """Lookup HLTV profile URL from ratings JSON by pipeline player key."""
    key = normalize_pipeline_player_key(player_key)
    for table in ratings.get("tables", []):
        for player in table.get("players", []):
            nick = normalize_pipeline_player_key(str(player.get("nickname", "")))
            if nick != key:
                continue
            url = str(player.get("hltv_player_url", "") or "").strip()
            if url:
                return _make_resolution(url, "ratings")
    return None


def parse_search_player_candidates(
    search_html: str,
    *,
    base_url: str = "https://www.hltv.org",
) -> list[SearchPlayerCandidate]:
    """Extract /player/ links from HLTV search page HTML."""
    soup = BeautifulSoup(search_html, "lxml")
    seen_ids: set[str] = set()
    candidates: list[SearchPlayerCandidate] = []

    for link in soup.find_all("a", href=True):
        href = str(link["href"])
        m = _HLTV_PLAYER_ID_RE.search(href)
        if not m:
            continue
        player_id = m.group(1)
        if player_id in seen_ids:
            continue
        seen_ids.add(player_id)

        slug_m = re.search(rf"/player/{player_id}/([^/?#]+)", href, re.IGNORECASE)
        slug = slug_m.group(1) if slug_m else ""
        text = link.get_text(strip=True)
        nickname = text or slug
        if not nickname:
            continue

        full_url = href if href.startswith("http") else urljoin(base_url, href)
        candidates.append(
            {
                "player_url": full_url,
                "player_id": player_id,
                "nickname": nickname,
            }
        )
    return candidates


def disambiguate_search_candidates(
    candidates: list[SearchPlayerCandidate],
    roster_nicknames: list[str],
    player_key: str,
) -> SearchPlayerCandidate | None:
    """Filter search hits to roster nicknames; tiebreak on exact player_key match."""
    key = normalize_pipeline_player_key(player_key)
    roster_set = set(roster_nicknames)
    if roster_set:
        on_roster = [
            candidate
            for candidate in candidates
            if normalize_pipeline_player_key(candidate["nickname"]) in roster_set
        ]
        if not on_roster:
            return None
        if len(on_roster) == 1:
            return on_roster[0]

        exact = [
            candidate
            for candidate in on_roster
            if normalize_pipeline_player_key(candidate["nickname"]) == key
        ]
        if len(exact) == 1:
            return exact[0]
        return None

    for candidate in candidates:
        slug = _player_slug_from_url(candidate["player_url"])
        if slug and _slug_matches(slug, key):
            return candidate
    return None


_HLTV_PLAYER_SLUG_RE = re.compile(r"/player/\d+/([^/?#]+)", re.IGNORECASE)


def _player_slug_from_url(url: str) -> str:
    m = _HLTV_PLAYER_SLUG_RE.search(url)
    return m.group(1).lower() if m else ""


def _slug_matches(slug: str, player_key: str) -> bool:
    return player_key == slug


def resolve_from_search(
    search_html: str,
    ratings: dict,
    player_key: str,
    *,
    base_url: str = "https://www.hltv.org",
) -> HltvPlayerResolution | None:
    """Disambiguate HLTV search results using ratings roster nicknames."""
    candidates = parse_search_player_candidates(search_html, base_url=base_url)
    roster = roster_nicknames_from_ratings(ratings)
    pick = disambiguate_search_candidates(candidates, roster, player_key)
    if pick is None:
        return None
    return _make_resolution(pick["player_url"], "search", pick["player_id"])


def resolve_from_roster(
    roster: list[dict],
    player_key: str,
) -> HltvPlayerResolution | None:
    """Lookup HLTV profile URL from match roster entries."""
    key = normalize_pipeline_player_key(player_key)
    for entry in roster:
        nick = normalize_pipeline_player_key(
            str(entry.get("nickname") or entry.get("nick") or "")
        )
        if nick != key:
            continue
        url = str(
            entry.get("playerUrl")
            or entry.get("player_url")
            or entry.get("hltv_player_url")
            or ""
        ).strip()
        if url:
            return _make_resolution(url, "roster")
    return None


def resolve_hltv_player(
    player_key: str,
    accounts: list[Any],
    ratings: dict,
    *,
    roster: list[dict] | None = None,
    search_html: str | None = None,
) -> HltvPlayerResolution | None:
    """Resolve HLTV profile in priority order: account → ratings → roster → search."""
    result = resolve_from_accounts(accounts, player_key)
    if result:
        return result
    result = resolve_from_ratings(ratings, player_key)
    if result:
        return result
    if roster:
        result = resolve_from_roster(roster, player_key)
        if result:
            return result
    if search_html:
        return resolve_from_search(search_html, ratings, player_key)
    return None


def load_ratings_json(ratings_path: Path | str) -> dict:
    """Load ratings JSON from disk; returns empty dict when missing or invalid."""
    path = Path(ratings_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def avatar_cache_eligible(png_path: Path, account: Any | None) -> bool:
    """True when cached PNG is large enough and account has a linked HLTV player ID."""
    if account is None:
        return False
    _, player_id, _ = _account_fields(account)
    if not player_id.strip():
        return False
    if not png_path.is_file():
        return False
    try:
        with Image.open(png_path) as im:
            # Size check
            if im.size[0] < MIN_AVATAR_RES or im.size[1] < MIN_AVATAR_RES:
                return False
            # Reject fully transparent images (e.g., HLTV CDN placeholder)
            if im.mode == "RGBA":
                alpha = im.getchannel("A")
                extrema = alpha.getextrema()
                if extrema[0] == 0 and extrema[1] == 0:
                    return False
            return True
    except OSError:
        return False
