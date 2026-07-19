"""
CS2Archive — FACEIT Demo Downloader (browser scrape)

No FACEIT API key required. Uses a persistent, logged-in Chrome profile
(`.faceit_profile/`, created via `scripts/faceit_login_launcher.py`) to open the
match room, click "Watch Demo", and capture the browser download to disk.

Cloudflare blocks automation browsers, so the launched context uses the system
Chrome channel with `--disable-blink-features=AutomationControlled` and reuses
the authenticated profile (which already holds the cf_clearance cookie).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console

from config import settings
from downloader import (
    build_demo_path, extract_demo, file_size_mb,
    is_already_downloaded, record_download,
)
from models import DemoSource, DownloadResult, DownloadStatus, MatchInfo

console = Console(force_terminal=True)

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".faceit_profile"


class FACEITClient:
    """FACEIT Data API v4 client — lookup only (player + match history).

    The free Data API key covers player lookup and match history. Demo
    download is handled separately by the browser-scrape path (no Downloads
    API token required).
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
        import asyncio
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

    async def get_player_id(self, nickname: str) -> Optional[str]:
        if not settings.has_faceit_key:
            console.print("[red]   [ERR] FACEIT API key not configured. See .env (FACEIT_API_KEY).[/red]")
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

    async def get_player_matches(
        self, player_id: str, limit: int = 20
    ) -> list[MatchInfo]:
        data = await self._get_client().get(
            f"/players/{player_id}/history",
            params={"game": "cs2", "offset": 0, "limit": limit},
        )
        data.raise_for_status()
        payload = data.json()
        matches = []
        for item in payload.get("items", []):
            match_id = item.get("match_id", "")
            teams = item.get("teams", {})
            team_names = [
                teams.get(f, {}).get("nickname", "Unknown")
                for f in ("faction1", "faction2")
            ]
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
                team1=team_names[0] if team_names else "Unknown",
                team2=team_names[1] if len(team_names) > 1 else "Unknown",
                date=date,
                url=f"https://www.faceit.com/en/cs2/room/{match_id}",
            ))
        return matches


# Button text fragments that trigger the demo download on a FACEIT room page.
DOWNLOAD_BUTTON_TEXTS = ["watch demo", "download demo", "download", "demo"]


def _resolve_match_id(room_or_id: str) -> str:
    """Accept a full room URL or a bare match id."""
    if "/room/" in room_or_id:
        return room_or_id.rstrip("/").split("/room/")[-1]
    return room_or_id.strip()


def _launch_context():
    """Launch the persistent, authenticated Chrome context."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR.resolve()),
        headless=False,
        channel="chrome",
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1920, "height": 1080},
        accept_downloads=True,
    )
    return pw, browser


def get_match_details(match_id: str) -> MatchInfo:
    """Scrape the room page for team names, map, and score (no API key)."""
    match_id = _resolve_match_id(match_id)
    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"

    pw, browser = _launch_context()
    try:
        page = browser.new_page()
        api_url = f"https://www.faceit.com/api/match/v4/match/{match_id}"
        api_payload = {}
        def _cap(r):
            if r.url == api_url:
                try:
                    api_payload.update(r.json().get("payload", {}))
                except Exception:
                    pass
        page.on("response", _cap)
        page.goto(room_url, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)

        team1, team2, map_name = "Unknown", "Unknown", "Unknown"
        if api_payload:
            t = api_payload.get("teams", {})
            team1 = t.get("faction1", {}).get("name", team1)
            team2 = t.get("faction2", {}).get("name", team2)
            voting = api_payload.get("voting", {})
            picks = voting.get("map", {}).get("pick", []) if voting else []
            if picks:
                picked = picks[0] if isinstance(picks[0], str) else picks[0].get("name", "")
                map_name = picked.replace("de_", "").strip() or map_name
        return MatchInfo(
            match_id=match_id, source=DemoSource.FACEIT,
            team1=team1, team2=team2, map_name=map_name,
            url=room_url,
        )
    finally:
        browser.close()
        pw.stop()


def _click_demo_and_save(page, out_dir: Path) -> Optional[Path]:
    """Click the demo download button and capture the browser download."""
    for txt in DOWNLOAD_BUTTON_TEXTS:
        try:
            loc = page.get_by_text(txt, exact=False)
            if loc.count() == 0:
                continue
            console.print(f"[cyan]   [DL] Clicking '{txt}'...[/cyan]")
            with page.expect_download(timeout=120_000) as dl_info:
                loc.first.click()
            dl = dl_info.value
            dest = out_dir / dl.suggested_filename
            dl.save_as(dest)
            return dest
        except Exception as e:
            console.print(f"[yellow]   [WARN] '{txt}' failed: {e}[/yellow]")
    return None


def download_demo(match_id: str) -> DownloadResult:
    """Full download flow: open room (authed) → click demo → extract → organize."""
    started = datetime.now()
    match_id = _resolve_match_id(match_id)
    match_info = MatchInfo(match_id=match_id, source=DemoSource.FACEIT)
    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"

    try:
        console.print(f"\n[bold cyan][>>] FACEIT match:[/bold cyan] {match_id}")

        existing = is_already_downloaded(match_id, DemoSource.FACEIT)
        if existing:
            console.print(f"[yellow]   [SKIP] Already downloaded: {existing}[/yellow]")
            return DownloadResult(
                match=match_info, status=DownloadStatus.SKIPPED,
                demo_path=existing, file_size_mb=file_size_mb(existing),
                started_at=started, completed_at=datetime.now(),
            )

        pw, browser = _launch_context()
        try:
            page = browser.new_page()
            api_url = f"https://www.faceit.com/api/match/v4/match/{match_id}"
            api_payload = {}
            def _cap(r):
                if r.url == api_url:
                    try:
                        api_payload.update(r.json().get("payload", {}))
                    except Exception:
                        pass
            page.on("response", _cap)
            page.goto(room_url, wait_until="domcontentloaded")
            console.print("[cyan]   [..] Waiting for Cloudflare + page load...[/cyan]")
            page.wait_for_timeout(8000)

            # enrich match info from the room's match API payload (no API key)
            if api_payload:
                t = api_payload.get("teams", {})
                match_info.team1 = t.get("faction1", {}).get("name", match_info.team1)
                match_info.team2 = t.get("faction2", {}).get("name", match_info.team2)
                voting = api_payload.get("voting", {})
                picks = voting.get("map", {}).get("pick", []) if voting else []
                if picks:
                    picked = picks[0] if isinstance(picks[0], str) else picks[0].get("name", "")
                    match_info.map_name = picked.replace("de_", "").strip() or match_info.map_name
            else:
                # fallback: parse map from visible page text
                try:
                    body = page.inner_text("body")
                    for line in body.splitlines():
                        ls = line.strip()
                        if ls in ("Dust2", "Mirage", "Inferno", "Nuke", "Ancient",
                                  "Anubis", "Overpass", "Vertigo", "Train"):
                            match_info.map_name = ls
                            break
                except Exception:
                    pass

            out_dir = settings.temp_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            saved = _click_demo_and_save(page, out_dir)

            if not saved:
                console.print("[red]   [ERR] Could not find a demo download button.[red]")
                return DownloadResult(
                    match=match_info, status=DownloadStatus.FAILED,
                    error="No demo download button found",
                    started_at=started, completed_at=datetime.now(),
                )

            console.print(f"[green]   [OK] Downloaded: {saved.name} ({file_size_mb(saved):.1f} MB)[/green]")
            console.print("[cyan]   [EXTRACT] Extracting .dem...[/cyan]")
            dem_paths = extract_demo(saved, settings.temp_dir)
            dem_path = dem_paths[0]

            organized = build_demo_path(match_info)
            organized.parent.mkdir(parents=True, exist_ok=True)
            if organized.exists():
                organized.unlink()
            dem_path.replace(organized)
            dem_path = organized

            record_download(DownloadResult(
                match=match_info, status=DownloadStatus.COMPLETED,
                demo_path=dem_path, file_size_mb=file_size_mb(dem_path),
                started_at=started, completed_at=datetime.now(),
            ))
            console.print(f"[bold green]   [DONE] Saved: {dem_path.name} ({file_size_mb(dem_path):.1f} MB)[/bold green]")
            return DownloadResult(
                match=match_info, status=DownloadStatus.COMPLETED,
                demo_path=dem_path, file_size_mb=file_size_mb(dem_path),
                started_at=started, completed_at=datetime.now(),
            )
        finally:
            browser.close()
            pw.stop()

    except Exception as e:
        console.print(f"[bold red]   [ERR] Error: {e}[/bold red]")
        return DownloadResult(
            match=match_info, status=DownloadStatus.FAILED,
            error=str(e), started_at=started, completed_at=datetime.now(),
        )
