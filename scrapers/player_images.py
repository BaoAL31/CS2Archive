"""
CS2Archive — Player Profile Image Scraper

Gets player avatars from HLTV match pages.
Uses ui-avatars.com since HLTV CDN blocks direct downloads.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from rich.console import Console

from config import settings

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    """Generate avatar images for all match participants."""
    from scrapers.hltv import HLTVScraper

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    scraper = HLTVScraper()
    try:
        html = await scraper._get_page_content(match_url)
        soup = BeautifulSoup(html, "lxml")

        player_links = set()
        for table in soup.select("table.totalstats"):
            for row in table.select("tr"):
                for link in row.select("a[href*='/player/']"):
                    href = link["href"]
                    m = re.match(r"/player/(\d+)/([^/?#]+)", href)
                    if m:
                        player_links.add((m.group(1), m.group(2)))

        if not player_links:
            console.print("[yellow]   No player links found[/yellow]")
            return result

        console.print(f"[dim]   Fetching {len(player_links)} avatars...[/dim]")

        for player_id, nickname in sorted(player_links):
            local_path = AVATAR_DIR / f"{nickname}.jpg"
            if local_path.exists():
                result[nickname] = local_path
                continue

            try:
                url = f"https://ui-avatars.com/api/?name={nickname}&size=200&background=random&format=png"
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                local_path.write_bytes(resp.content)
                result[nickname] = local_path
                console.print(f"[green]   [OK] {nickname}.jpg ({len(resp.content) / 1024:.0f} KB)[/green]")
            except Exception as e:
                console.print(f"[yellow]   [{nickname}] {e}[/yellow]")

        return result
    finally:
        await scraper.close()
