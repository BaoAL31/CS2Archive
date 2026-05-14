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


async def get_match_ratings(match_url: str) -> Optional[dict]:
    """Scrape HLTV match page for all player ratings and stats.

    Returns dict with match info and per-map player stats.
    """
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        html = await scraper._get_page_content(match_url)
        soup = BeautifulSoup(html, "lxml")

        match_name_el = soup.select_one(".match-header-title")
        match_name = match_name_el.get_text(strip=True) if match_name_el else "Unknown Match"

        series_stats: list[dict] = []
        tables = soup.select("table.totalstats")

        current_map = "Overall"
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
                    team_players.append({
                        "nickname": nickname,
                        "kd": kd,
                        "swing": swing or "",
                        "adr": adr or "",
                        "kast": kast or "",
                        "rating": rating,
                    })

            if team_players:
                series_stats.append({
                    "map": current_map,
                    "team": team_name or "",
                    "players": team_players,
                })

        return {
            "match_name": match_name,
            "url": match_url,
            "tables": series_stats,
        }

    finally:
        await scraper.close()
