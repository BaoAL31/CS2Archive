"""
CS2Archive — HLTV Ratings Scraper

Scrapes HLTV match pages for exact player statistics and HLTV Rating 3.0.
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import BeautifulSoup
from rich.console import Console

from config import settings

console = Console(force_terminal=True)

_HLTV_PLAYER_PATH = re.compile(r"/player/\d+/[^/?#]+", re.IGNORECASE)


def _normalize_hltv_player_url(href: str | None) -> str | None:
    """Return canonical HLTV profile URL from a stats-table link href."""
    if not href:
        return None
    m = _HLTV_PLAYER_PATH.search(href)
    if not m:
        return None
    path = m.group(0)
    if href.startswith("http"):
        return href.split("?", 1)[0]
    return f"https://www.hltv.org{path}"


def _hltv_player_url_from_row(row) -> str | None:
    """Extract profile URL from a totalstats player row when present."""
    for link in row.select('a[href*="/player/"]'):
        url = _normalize_hltv_player_url(link.get("href"))
        if url:
            return url
    return None


def parse_match_ratings_html(html: str, match_url: str = "") -> Optional[dict]:
    """Parse HLTV match page HTML for all player ratings and stats."""
    soup = BeautifulSoup(html, "lxml")

    match_name_el = soup.select_one(".match-header-title")
    match_name = match_name_el.get_text(strip=True) if match_name_el else "Unknown Match"

    match_stage = ""
    stage_el = soup.select_one("div.map-info-wrap ul li")
    if not stage_el:
        stage_el = soup.select_one("div.match-info-box div.text")
    if not stage_el:
        m = re.search(r"\*\s*(.+?(?:final|playoff|group|stage|qualifier|round|decider|match))", html, re.IGNORECASE)
        if m:
            match_stage = m.group(1).strip()
    else:
        match_stage = stage_el.get_text(strip=True)

    map_names: dict[str, str] = {}
    for el in soup.find_all("div", class_="dynamic-map-name-full"):
        cid = el.get("id", "")
        if cid:
            map_names[cid] = el.get_text(strip=True)
    map_names["all"] = "Series Overall"

    series_stats: list[dict] = []
    tables = soup.select("table.totalstats")

    for table in tables:
        parent = table.find_parent(["div", "section"])
        if parent and parent.select_one(".stats-tabs, .stats-tab-content"):
            continue

        rows = table.select("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].select("th, td")
        header_texts = [c.get_text(strip=True).lower() for c in header_cells]
        header_text = " ".join(header_texts)

        if "rating3.0" not in header_text and "rating 3.0" not in header_text:
            continue

        td = table.select_one("td")
        team_name = td.get_text(strip=True) if td else ""

        parent_id = table.parent.get("id", "") if table.parent else ""
        content_id = parent_id.replace("-content", "")
        map_name = map_names.get(content_id, content_id if content_id and content_id != "all" else "Series Overall")

        team_players = []
        for row in rows[1:]:
            cells = row.select("td")
            if len(cells) < 3:
                continue

            text = row.get_text(" ", strip=True)

            name_m = re.search(r"'([^']+)'", text)
            nickname = name_m.group(1) if name_m else cells[0].get_text(strip=True) if len(cells) > 0 else ""

            kd_m = re.search(r'(\d+[–\-]\d+)', text)
            kd = kd_m.group(1) if kd_m else ""

            swing_m = re.search(r'([+\-]\d+\.\d+%)', text)
            swing = swing_m.group(1) if swing_m else ""

            adr_m = re.search(r'(\d+\.\d)\s+(?:\d+\.\d\s+)?(\d+\.\d%)', text)
            adr = ""
            if adr_m:
                adr = adr_m.group(1)

            kast_m = re.search(r'(\d+\.\d%)', text)
            kast = ""
            if kast_m:
                kast = kast_m.group(1)

            rating = ""
            for cell in cells:
                ct = cell.get_text(strip=True)
                try:
                    r = float(ct)
                    if 0.0 <= r <= 3.0:
                        rating = ct
                except ValueError:
                    continue

            if nickname and rating:
                player: dict[str, str] = {
                    "nickname": nickname,
                    "kd": kd,
                    "swing": swing or "",
                    "adr": adr or "",
                    "kast": kast or "",
                    "rating": rating,
                }
                hltv_player_url = _hltv_player_url_from_row(row)
                if hltv_player_url:
                    player["hltv_player_url"] = hltv_player_url
                team_players.append(player)

        if team_players:
            series_stats.append({
                "map": map_name,
                "team": team_name or "",
                "players": team_players,
            })

    return {
        "match_name": match_name,
        "url": match_url,
        "match_stage": match_stage,
        "tables": series_stats,
    }


async def get_match_ratings(match_url: str) -> Optional[dict]:
    """Scrape HLTV match page for all player ratings and stats.

    Returns dict with match info and per-map player stats.
    """
    import asyncio

    from scrapers.hltv_acquire import fetch_hltv_page_html

    html = await asyncio.to_thread(
        fetch_hltv_page_html,
        match_url,
        wait_selector="table.totalstats, .match-header-title",
    )
    result = parse_match_ratings_html(html, match_url)
    if not result or not result.get("tables"):
        return None
    return result
