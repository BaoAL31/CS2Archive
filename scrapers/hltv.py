"""
CS2Archive — HLTV Scraper

Scrapes HLTV.org match pages to extract GOTV demo download links.
Uses CloakBrowser with persistent profile to bypass Cloudflare protection.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext
from playwright.sync_api import sync_playwright
from rich.console import Console

from config import settings
from models import DemoSource, DownloadResult, DownloadStatus, MatchInfo

console = Console(force_terminal=True)

CLOAK_PROFILE = Path(".sessions/hltv-cloak")


class HLTVScraper:
    """Scrapes HLTV.org for CS2 match data and GOTV demo downloads."""

    def __init__(self, *, headless: bool = False):
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._headless = headless

    async def _ensure_browser(self) -> Browser:
        if self._browser:
            return self._browser
        from scrapers.hltv_acquire import DEFAULT_PROFILE_DIR

        DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        pw = await async_playwright().start()
        self._browser = await pw.chromium.launch(
            channel="chrome",
            headless=self._headless,
            ignore_default_args=["--enable-automation"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        return self._browser

    async def _ensure_context(self) -> BrowserContext:
        await self._ensure_browser()
        return self._context

    @property
    def browser(self) -> Browser | None:
        return self._browser

    async def fresh_context(self) -> BrowserContext:
        browser = await self._ensure_browser()
        return await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            bypass_csp=True,
            extra_http_headers={"Cache-Control": "no-cache, no-store"},
        )

    async def navigate(self, url: str, *, timeout_ms: int = 30000) -> str:
        """Rate-limited navigation in a single reusable page. Returns page content."""
        await self._ensure_browser()
        if not hasattr(self, "_nav_page") or self._nav_page is None:
            self._nav_page = await self._context.new_page()
        await self._rate_limit()
        try:
            await self._nav_page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await self._nav_page.wait_for_timeout(2000)
            return await self._nav_page.content()
        except Exception:
            try:
                await self._nav_page.close()
            except Exception:
                pass
            self._nav_page = None
            raise

    async def close(self) -> None:
        if hasattr(self, "_nav_page") and self._nav_page:
            try:
                await self._nav_page.close()
            except Exception:
                pass
            self._nav_page = None
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        self._context = None
        self._browser = None

    async def _rate_limit(self) -> None:
        delay = random.uniform(settings.hltv_request_delay_min, settings.hltv_request_delay_max)
        await asyncio.sleep(delay)

    async def _get_page_content(self, url: str) -> str:
        from scrapers.hltv_acquire import fetch_hltv_page_html

        return await asyncio.to_thread(fetch_hltv_page_html, url)

    async def get_match_info(self, match_url: str) -> MatchInfo:
        """Parse match metadata from an HLTV match page without downloading."""
        html = await self._get_page_content(match_url)
        soup = BeautifulSoup(html, "lxml")
        return self._parse_match_info(soup, match_url)

    async def get_match_demo(
        self,
        match_url: str,
        *,
        force: bool = False,
        headless: bool = False,
        profile_dir: Path | None = None,
    ) -> DownloadResult:
        """Download and extract GOTV demo via CloakBrowser (see scrapers.hltv_acquire)."""
        from scrapers.hltv_acquire import acquire_match

        return await asyncio.to_thread(
            acquire_match,
            match_url,
            force=force,
            headless=headless,
            profile_dir=profile_dir,
        )

    async def search_player_matches(self, player_name: str, count: int = 5, steam_id: str = "") -> list[MatchInfo]:
        """Search for a player's recent matches on HLTV.

        Uses steam_id for more accurate search when available,
        falls back to name-based search.
        """
        query = steam_id if steam_id else player_name
        label = f"{player_name} (Steam: {steam_id})" if steam_id else player_name
        console.print(f"\n[bold cyan][>>] Searching HLTV for player:[/bold cyan] {label}")
        search_url = f"{settings.hltv_base_url}/search?query={query}"
        html = await self._get_page_content(search_url)
        soup = BeautifulSoup(html, "lxml")

        player_link = None
        search_text = player_name.lower()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/player/" in href and search_text in link.get_text().lower():
                player_link = urljoin(settings.hltv_base_url, href)
                break

        if not player_link:
            console.print(f"[red]   [ERR] Player '{player_name}' not found on HLTV[/red]")
            return []

        console.print(f"[green]   [OK] Found player page: {player_link}[/green]")
        await self._rate_limit()

        player_html = await self._get_page_content(player_link)
        player_soup = BeautifulSoup(player_html, "lxml")

        matches = []
        match_links = set()
        for link in player_soup.find_all("a", href=True):
            href = link["href"]
            if "/matches/" in href and href not in match_links:
                if re.match(r".*/matches/\d+/.*", href):
                    match_links.add(href)
                    full_url = urljoin(settings.hltv_base_url, href)
                    mid = re.search(r"/matches/(\d+)/", href)
                    matches.append(MatchInfo(
                        match_id=mid.group(1) if mid else href,
                        source=DemoSource.HLTV, url=full_url,
                    ))
                    if len(matches) >= count:
                        break

        console.print(f"[green]   [OK] Found {len(matches)} match(es)[/green]")
        return matches

    async def search_event_matches(self, event_url: str) -> list[MatchInfo]:
        """Get all matches from an HLTV event/tournament page."""
        console.print(f"\n[bold cyan][>>] Scraping event matches:[/bold cyan] {event_url}")
        html = await self._get_page_content(event_url)
        soup = BeautifulSoup(html, "lxml")

        matches = []
        match_links = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/matches/" in href and href not in match_links:
                if re.match(r".*/matches/\d+/.*", href):
                    match_links.add(href)
                    full_url = urljoin(settings.hltv_base_url, href)
                    mid = re.search(r"/matches/(\d+)/", href)
                    if mid:
                        matches.append(MatchInfo(
                            match_id=mid.group(1), source=DemoSource.HLTV, url=full_url,
                        ))

        console.print(f"[green]   [OK] Found {len(matches)} match(es) in event[/green]")
        return matches

    async def search_matches_by_query(self, query: str, count: int = 5) -> list[MatchInfo]:
        """Search HLTV for matches matching a query (e.g. 'Liquid vs M80')."""
        console.print(f"\n[bold cyan][>>] Searching HLTV:[/bold cyan] {query}")
        search_url = f"{settings.hltv_base_url}/search?query={query}"
        html = await self._get_page_content(search_url)
        soup = BeautifulSoup(html, "lxml")

        query_teams = [t.strip().lower() for t in re.split(r"\s+vs\.?\s+", query, maxsplit=1)]

        all_match_links = []
        seen = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            m = re.match(r".*/matches/(\d+)/(.*)", href)
            if not m or href in seen:
                continue
            seen.add(href)
            text = link.get_text(" ", strip=True).lower()
            all_match_links.append((link, href, m.group(1), m.group(2), text))

        ranked = []
        for link, href, mid, slug, link_text in all_match_links:
            slug_lower = slug.lower()
            score = 0
            for qt in query_teams:
                if qt in slug_lower:
                    score += 2
                if qt in link_text:
                    score += 1
            if score > 0:
                ranked.append((score, href, mid, slug))

        ranked.sort(key=lambda x: -x[0])

        if not ranked:
            for _, href, mid, slug, _ in all_match_links[:count]:
                ranked.append((0, href, mid, slug))

        matches = []
        for score, href, mid, slug in ranked[:count]:
            display = slug.replace("-vs-", " vs ").replace("-", " ").title()
            full_url = urljoin(settings.hltv_base_url, href)
            matches.append(MatchInfo(
                match_id=mid, source=DemoSource.HLTV, url=full_url, team1=display,
            ))

        if matches:
            console.print(f"[green]   [OK] Found {len(matches)} match(es)[/green]")
        return matches

    # ── HTML Parsing Helpers ──────────────────────────────────────────────

    def _parse_match_info(self, soup: BeautifulSoup, url: str) -> MatchInfo:
        match_id = ""
        id_match = re.search(r"/matches/(\d+)/", url)
        if id_match:
            match_id = id_match.group(1)

        team_names = [el.get_text(strip=True) for el in soup.select(".teamName")]
        team1 = team_names[0] if len(team_names) > 0 else "Unknown"
        team2 = team_names[1] if len(team_names) > 1 else "Unknown"

        score = ""
        score_els = soup.select(".team1-gradient .won, .team2-gradient .won, "
                                ".team1-gradient .lost, .team2-gradient .lost")
        if len(score_els) >= 2:
            score = f"{score_els[0].get_text(strip=True)}-{score_els[1].get_text(strip=True)}"

        map_name = "Unknown"
        map_el = soup.select_one(".mapName")
        if map_el:
            map_name = map_el.get_text(strip=True)

        event = ""
        event_el = soup.select_one(".event a")
        if event_el:
            event = event_el.get_text(strip=True)

        date = None
        date_el = soup.select_one("[data-unix]")
        if date_el:
            try:
                date = datetime.fromtimestamp(int(date_el["data-unix"]) / 1000)
            except (ValueError, KeyError):
                pass

        return MatchInfo(
            match_id=match_id, source=DemoSource.HLTV,
            team1=team1, team2=team2, score=score, map_name=map_name,
            date=date, event=event, url=url,
        )

    def _extract_demo_id(self, soup: BeautifulSoup) -> Optional[str]:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            m = re.search(r"/download/demo/(\d+)", href)
            if m:
                return m.group(1)
            if "demoid=" in href:
                m = re.search(r"demoid=(\d+)", href)
                if m:
                    return m.group(1)
        for el in soup.find_all(attrs={"data-demoid": True}):
            return el["data-demoid"]
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r"/download/demo/(\d+)", text)
            if m:
                return m.group(1)
            m = re.search(r"demoid['\"]?\s*[:=]\s*['\"]?(\d+)", text)
            if m:
                return m.group(1)
        return None

    def _extract_cdn_url(self, soup: BeautifulSoup) -> Optional[str]:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "r2-demos.hltv.org" in href:
                return href
        for script in soup.find_all("script"):
            text = script.string or ""
            m = re.search(r'(https?://r2-demos\.hltv\.org[^\s"\'<>]+)', text)
            if m:
                return m.group(1)
        return None

    def _detect_and_rename_archive(self, path: Path) -> Path:
        with open(path, "rb") as f:
            magic = f.read(8)
        if magic[:4] == b"Rar!":
            new_path = path.with_suffix(".rar")
        elif magic[:2] == b"PK":
            new_path = path.with_suffix(".zip")
        elif magic[:2] == b"\x1f\x8b":
            new_path = path.with_suffix(".dem.gz")
        elif magic[:8] == b"HL2DEMO\x00":
            new_path = path.with_suffix(".dem")
        else:
            new_path = path.with_suffix(".rar")
        if new_path != path:
            path.rename(new_path)
        return new_path
