"""
CS2Archive — FACEIT API Client

Uses the official FACEIT Data API v4 to find matches and download demos.
Demo downloads require a separate Downloads API token (applied separately).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console

from config import settings
from downloader import (
    build_demo_path, cleanup_temp, download_file, extract_demo, file_size_mb,
    is_already_downloaded, record_download,
)
from models import DemoSource, DownloadResult, DownloadStatus, MatchInfo

console = Console(force_terminal=True)


class FACEITClient:
    """
    FACEIT Data API v4 client for CS2 match data and demo downloads.

    The Data API (player lookup, match history) works with a free API key.
    The Downloads API (actual demo file download) requires a separate token.
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.faceit_data_api_base,
                headers={
                    "Authorization": f"Bearer {settings.faceit_api_key}",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(30.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        """Make an API request with retry logic."""
        client = self._get_client()
        last_error = None

        for attempt in range(3):
            try:
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_error = e
                if e.response.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    console.print(f"[yellow]   [WAIT] Rate limited, waiting {wait}s...[/yellow]")
                    await asyncio.sleep(wait)
                elif e.response.status_code >= 500:
                    await asyncio.sleep(1)
                else:
                    raise
            except httpx.RequestError as e:
                last_error = e
                await asyncio.sleep(1)

        raise last_error  # type: ignore

    # ── Player Lookup ─────────────────────────────────────────────────────

    async def get_player_id(self, nickname: str) -> Optional[str]:
        """Look up a player's FACEIT ID by nickname."""
        if not settings.has_faceit_key:
            console.print("[red]   [ERR] FACEIT API key not configured. See .env.example[/red]")
            return None

        try:
            data = await self._request("GET", "/players", params={
                "nickname": nickname, "game": "cs2",
            })
            player_id = data.get("player_id")
            if player_id:
                console.print(f"[green]   [OK] Found player: {data.get('nickname', nickname)} "
                              f"(Level {data.get('games', {}).get('cs2', {}).get('skill_level', '?')})[/green]")
            return player_id
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                console.print(f"[red]   [ERR] Player '{nickname}' not found on FACEIT[/red]")
                return None
            raise

    # ── Match History ─────────────────────────────────────────────────────

    async def get_player_matches(
        self, player_id: str, limit: int = 20
    ) -> list[MatchInfo]:
        """Fetch a player's recent CS2 match history."""
        data = await self._request("GET", f"/players/{player_id}/history", params={
            "game": "cs2", "offset": 0, "limit": limit,
        })

        matches = []
        for item in data.get("items", []):
            match_id = item.get("match_id", "")
            teams = item.get("teams", {})
            team_names = []
            for faction in ["faction1", "faction2"]:
                team_data = teams.get(faction, {})
                team_names.append(team_data.get("nickname", "Unknown"))

            started = item.get("started_at")
            date = None
            if started:
                try:
                    date = datetime.fromtimestamp(started)
                except (ValueError, TypeError):
                    pass

            matches.append(MatchInfo(
                match_id=match_id,
                source=DemoSource.FACEIT,
                team1=team_names[0] if len(team_names) > 0 else "Unknown",
                team2=team_names[1] if len(team_names) > 1 else "Unknown",
                date=date,
                url=f"https://www.faceit.com/en/cs2/room/{match_id}",
            ))

        return matches

    # ── Match Details ─────────────────────────────────────────────────────

    async def get_match_details(self, match_id: str) -> MatchInfo:
        """Get full match details including demo URL."""
        data = await self._request("GET", f"/matches/{match_id}")

        teams = data.get("teams", {})
        team1 = teams.get("faction1", {}).get("name", "Unknown")
        team2 = teams.get("faction2", {}).get("name", "Unknown")

        # Extract map and score from results
        results = data.get("results", {})
        score = ""
        if results:
            s1 = results.get("score", {}).get("faction1", "")
            s2 = results.get("score", {}).get("faction2", "")
            if s1 and s2:
                score = f"{s1}-{s2}"

        # Map name from voting or match details
        map_name = "Unknown"
        voting = data.get("voting", {})
        if voting:
            map_picks = voting.get("map", {}).get("pick", [])
            if map_picks:
                map_name = map_picks[0] if isinstance(map_picks[0], str) else map_picks[0].get("name", "Unknown")

        # Demo URL
        demo_url = ""
        demo_urls = data.get("demo_url", [])
        if isinstance(demo_urls, list) and demo_urls:
            demo_url = demo_urls[0]
        elif isinstance(demo_urls, str):
            demo_url = demo_urls

        date = None
        started = data.get("started_at")
        if started:
            try:
                date = datetime.fromtimestamp(started)
            except (ValueError, TypeError):
                pass

        event = data.get("competition_name", "")

        return MatchInfo(
            match_id=match_id, source=DemoSource.FACEIT,
            team1=team1, team2=team2, score=score, map_name=map_name,
            date=date, event=event, demo_url=demo_url,
            url=f"https://www.faceit.com/en/cs2/room/{match_id}",
        )

    # ── Demo Download ─────────────────────────────────────────────────────

    async def download_demo(self, match_id: str) -> DownloadResult:
        """
        Full download flow: get match details → get signed URL → download → extract.
        """
        started = datetime.now()
        match_info = MatchInfo(match_id=match_id, source=DemoSource.FACEIT)

        try:
            console.print(f"\n[bold cyan][>>] Fetching FACEIT match:[/bold cyan] {match_id}")

            # Get match details
            match_info = await self.get_match_details(match_id)
            console.print(f"[green]   [OK] {match_info.display_name}[/green]")

            # Check if already downloaded
            existing = is_already_downloaded(match_id, DemoSource.FACEIT)
            if existing:
                console.print(f"[yellow]   [SKIP] Already downloaded: {existing}[/yellow]")
                return DownloadResult(
                    match=match_info, status=DownloadStatus.SKIPPED,
                    demo_path=existing, file_size_mb=file_size_mb(existing),
                    started_at=started, completed_at=datetime.now(),
                )

            if not match_info.demo_url:
                raise ValueError("No demo URL available for this match (may have expired — 30 day limit)")

            # Get signed download URL via Downloads API
            download_url = await self._get_signed_url(match_info.demo_url)

            # Download the compressed demo
            temp_path = settings.temp_dir / f"faceit_{match_id}.dem.zst"
            console.print("[cyan]   [DL] Downloading demo...[/cyan]")
            await download_file(url=download_url, dest=temp_path, description=f"FACEIT {match_id[:8]}...")

            # Extract
            console.print("[cyan]   [EXTRACT] Extracting .dem file...[/cyan]")
            dem_path = extract_demo(temp_path, settings.temp_dir)
            cleanup_temp(temp_path)

            import shutil
            organized_path = build_demo_path(match_info)
            organized_path.parent.mkdir(parents=True, exist_ok=True)
            if organized_path.exists():
                organized_path.unlink()
            shutil.move(str(dem_path), str(organized_path))
            dem_path = organized_path

            result = DownloadResult(
                match=match_info, status=DownloadStatus.COMPLETED,
                demo_path=dem_path, file_size_mb=file_size_mb(dem_path),
                started_at=started, completed_at=datetime.now(),
            )
            record_download(result)
            console.print(f"[bold green]   [DONE] Saved: {dem_path.name} ({result.file_size_mb:.1f} MB)[/bold green]")
            return result

        except Exception as e:
            console.print(f"[bold red]   [ERR] Error: {e}[/bold red]")
            return DownloadResult(
                match=match_info, status=DownloadStatus.FAILED,
                error=str(e), started_at=started, completed_at=datetime.now(),
            )

    async def _get_signed_url(self, resource_url: str) -> str:
        """Call the FACEIT Downloads API to get a signed download URL."""
        if not settings.has_faceit_downloads:
            console.print(
                "[yellow]   [WARN] No Downloads API token configured.[/yellow]\n"
                "[yellow]     Apply at: https://fce.gg/downloads-api-application[/yellow]\n"
                "[yellow]     Attempting direct download...[/yellow]"
            )
            return resource_url

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                settings.faceit_downloads_api_url,
                json={"resource_url": resource_url},
                headers={
                    "Authorization": f"Bearer {settings.faceit_downloads_token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["payload"]["download_url"]
