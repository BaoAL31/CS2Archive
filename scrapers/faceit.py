"""
CS2Archive — FACEIT Demo Downloader (browser scrape)

No FACEIT API key required. Uses a persistent, logged-in Chrome profile
(`.sessions/faceit/`, created via `scripts/faceit/faceit_login_launcher.py`) to open the
match room, click "Watch Demo", and capture the browser download to disk.

Cloudflare blocks automation browsers, so the launched context uses the system
Chrome channel with `--disable-blink-features=AutomationControlled` and reuses
the authenticated profile (which already holds the cf_clearance cookie).
"""

from __future__ import annotations

import re
import time
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

PROFILE_DIR = Path(__file__).resolve().parent.parent / ".sessions/faceit"


class FACEITClient:
    """FACEIT Data API v4 client — lookup only (player + match history).

    The free Data API key covers player lookup and match history. Demo
    download is handled separately by the browser-scrape path (no Downloads
    API token required).
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._elo_cache: dict[str, int] = {}
        self._elo_by_steam: dict[str, int] = {}

    async def get_elo_by_steam_id(self, steam_id: str) -> Optional[int]:
        """Current FACEIT ELO for a player, looked up by steam64.

        Single ``/players?game=cs2&game_player_id=<steam64>`` call — the
        response carries ``games.cs2.faceit_elo``. Cached per steam id.
        """
        if steam_id in self._elo_by_steam:
            return self._elo_by_steam[steam_id]
        try:
            data = await self._request("GET", "/players", params={
                "game": "cs2", "game_player_id": steam_id,
            })
            elo = data.get("games", {}).get("cs2", {}).get("faceit_elo")
            if elo is not None:
                self._elo_by_steam[steam_id] = int(elo)
                return self._elo_by_steam[steam_id]
        except Exception:
            pass
        return None

    async def get_player_elo(self, player_id: str) -> Optional[int]:
        """FACEIT ELO for a player (cached). None on failure."""
        if player_id in self._elo_cache:
            return self._elo_cache[player_id]
        try:
            data = await self._request("GET", f"/players/{player_id}")
            elo = data.get("games", {}).get("cs2", {}).get("faceit_elo")
            if elo is not None:
                self._elo_cache[player_id] = int(elo)
                return self._elo_cache[player_id]
        except Exception:
            pass
        return None

    async def get_player_steam_id(self, player_id: str) -> Optional[str]:
        """steam_id_64 for a FACEIT player_id. None on failure."""
        if player_id in self._elo_cache:
            # elo cache keyed by player_id; we keep a separate steam cache
            pass
        try:
            data = await self._request("GET", f"/players/{player_id}")
            sid = data.get("steam_id_64")
            return str(sid) if sid else None
        except Exception:
            return None

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

    async def get_match_stats(self, match_id: str) -> Optional[dict]:
        """FACEIT match stats: map, score, winner, per-player stat lines.

        Returns {"map": str, "score": str, "teams": {team: score},
                 "players": {nickname: {kills, deaths, kd, adr, hs, ...}}}
        or None on failure.
        """
        try:
            data = await self._get_client().get(f"/matches/{match_id}/stats")
            data.raise_for_status()
            payload = data.json()
        except Exception as e:
            console.print(f"[yellow]   [WARN] stats failed for {match_id}: {e}[/yellow]")
            return None
        out = {"map": "Unknown", "score": "", "teams": {}, "players": {}}
        for rnd in payload.get("rounds", []):
            rs = rnd.get("round_stats", {})
            out["map"] = rs.get("Map", out["map"]).replace("de_", "")
            out["score"] = rs.get("Score", out["score"])
            for team in rnd.get("teams", []):
                tname = team.get("team_stats", {}).get("Team", "Unknown")
                tscore = team.get("team_stats", {}).get("Final Score", "?")
                out["teams"][tname] = tscore
                for p in team.get("players", []):
                    ps = p.get("player_stats", {})
                    out["players"][p.get("nickname", "?")] = {
                        "kills": ps.get("Kills", "?"),
                        "deaths": ps.get("Deaths", "?"),
                        "kd": ps.get("K/D Ratio", "?"),
                        "adr": ps.get("ADR", "?"),
                        "hs": ps.get("Headshots %", "?"),
                        "result": ps.get("Result", "?"),
                        "player_id": p.get("player_id"),
                    }
        return out


# Button text fragments that trigger the demo download on a FACEIT room page.
class FACEITDownloadsClient:
    """FACEIT Downloads API client (token-based, no browser).

    Endpoint: POST https://api.faceit.com/download/v2/demos/download
    Body: {"resource_url": "<demo_url from match payload>"}
    Auth: Bearer FACEIT_DOWNLOADS_TOKEN (requires Downloads API scope).
    Returns: {"payload": {"download_url": "<signed url>"}}

    The resource_url comes from the match payload's `demo_url` field
    (a list of CDN URLs in the current API). Pass the first element.
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.faceit_downloads_api_base,
                headers={
                    "Authorization": f"Bearer {settings.faceit_downloads_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_signed_url(self, resource_url: str) -> Optional[str]:
        """Exchange a demo resource_url for a time-limited signed download URL.

        Returns None on auth failure (400 invalid_token) or server error (500).
        """
        if not settings.has_faceit_downloads_token:
            console.print("[yellow]   [WARN] FACEIT_DOWNLOADS_TOKEN not set; use browser scrape.[/yellow]")
            return None
        try:
            client = self._get_client()
            resp = await client.post("/demos/download",
                                   json={"resource_url": resource_url})
            if resp.status_code == 400:
                console.print("[red]   [ERR] Downloads token rejected (invalid_token). Check scope.[/red]")
                return None
            if resp.status_code != 200:
                console.print(f"[red]   [ERR] Downloads API {resp.status_code}: {resp.text[:160]}[/red]")
                return None
            return resp.json().get("payload", {}).get("download_url")
        except Exception as e:
            console.print(f"[red]   [ERR] Downloads API request failed: {e}[/red]")
            return None

    async def download_match(self, match_id: str, out_dir: Path) -> Optional[Path]:
        """Resolve demo_url via Data API, get signed URL, stream to disk."""
        from downloader import file_size_mb
        # 1. fetch match payload for demo_url
        try:
            async with httpx.AsyncClient(
                base_url=settings.faceit_data_api_base,
                headers={"Authorization": f"Bearer {settings.faceit_api_key}"},
                timeout=30.0,
            ) as data:
                m = await data.get(f"/matches/{match_id}")
                m.raise_for_status()
                payload = m.json()
        except Exception as e:
            console.print(f"[red]   [ERR] Match lookup failed: {e}[/red]")
            return None
        demo_url = payload.get("demo_url")
        if isinstance(demo_url, list):
            demo_url = demo_url[0] if demo_url else None
        if not demo_url:
            console.print("[red]   [ERR] No demo_url in match payload.[/red]")
            return None

        signed = await self.get_signed_url(demo_url)
        if not signed:
            return None

        # 2. stream the signed url
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = demo_url.rsplit("/", 1)[-1]
        dest = out_dir / fname
        try:
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as dl:
                async with dl.stream("GET", signed) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in r.aiter_bytes(8192):
                            f.write(chunk)
            console.print(f"[green]   [OK] Downloaded: {dest.name} ({file_size_mb(dest):.1f} MB)[/green]")
            return dest
        except Exception as e:
            console.print(f"[red]   [ERR] Stream failed: {e}[/red]")
            if dest.exists():
                dest.unlink()
            return None


def download_demo_api(match_id: str) -> Optional[Path]:
    """Download a FACEIT demo via the official Downloads API (token-based)."""
    import asyncio
    out_dir = settings.temp_dir
    client = FACEITDownloadsClient()
    return asyncio.run(client.download_match(match_id, out_dir))


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


DOWNLOAD_START_TIMEOUT = 10  # seconds to wait for the download to begin
DOWNLOAD_MAX_RETRIES = 3     # restart attempts if the download doesn't start


def _find_demo_button(page, txt: str):
    """Locate the FACEIT 'Watch Demo' button via several selector strategies.
    Returns a locator or None if not found."""
    strategies = [
        lambda: page.get_by_text(txt, exact=False),
        lambda: page.locator(f"a:has-text('{txt}')"),
        lambda: page.get_by_role("link", name=txt),
        lambda: page.get_by_role("button", name=txt),
        lambda: page.locator(f"a[href*='{txt}']"),
        lambda: page.locator("a[href*='demo'], a[href*='download']"),
    ]
    for strat in strategies:
        try:
            loc = strat()
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def _click_demo_and_save(page, out_dir: Path, room_url: Optional[str] = None) -> Optional[Path]:
    """Click the demo download button and capture the browser download.

    WATCH DEMO opens a popup that initiates the download — the download event
    fires at the browser level, not on the page, so we use CDP
    Browser.setDownloadBehavior to route it into out_dir natively, then wait
    for the file to appear (the .crdownload suffix disappears on completion).

    If the download doesn't start within DOWNLOAD_START_TIMEOUT of the click,
    the attempt is restarted (page re-navigated to room_url, modal re-dismissed,
    button re-clicked), up to DOWNLOAD_MAX_RETRIES times.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(out_dir.resolve()), "eventsEnabled": True},
        )
    except Exception as e:
        console.print(f"[yellow]   [WARN] CDP download setup failed: {e}[/yellow]")

    before = set(out_dir.iterdir())

    def _new_files() -> list[Path]:
        return [p for p in out_dir.iterdir() if p.is_file() and p not in before]

    def _download_started() -> Optional[Path]:
        """True once any new file appears — .crdownload counts as started."""
        files = _new_files()
        if not files:
            return None
        # prefer a completed file; otherwise report the in-progress one
        done = [p for p in files if p.suffix != ".crdownload"]
        return (done or files)[0]

    def _reload() -> bool:
        """Re-navigate to the room page; returns False if impossible."""
        if not room_url:
            return False
        try:
            page.goto(room_url, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            return True
        except Exception as e:
            console.print(f"[yellow]   [WARN] Reload failed: {e}[/yellow]")
            return False

    for txt in DOWNLOAD_BUTTON_TEXTS:
        for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
            try:
                if attempt > 1 and not _reload():
                    return None
                loc = _find_demo_button(page, txt)
                if loc is None:
                    break  # try next button text

                console.print(f"[cyan]   [DL] Clicking '{txt}' (attempt {attempt})...[/cyan]")
                # Dismiss any interstitial modal (e.g. "Season recap") that
                # would intercept pointer events on the demo button.
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(600)
                except Exception:
                    pass
                # FACEIT's Watch Demo opens a popup that triggers the download.
                # A Playwright element .click() doesn't always open it (and just
                # navigates/loads), so we dispatch a REAL mouse gesture at the
                # button center and catch the popup, then let the download fire.
                popup = None
                try:
                    bbox = loc.first.bounding_box()
                except Exception:
                    bbox = None
                if bbox:
                    try:
                        with page.context.expect_page(timeout=8000) as pinfo:
                            page.mouse.click(
                                bbox["x"] + bbox["width"] / 2,
                                bbox["y"] + bbox["height"] / 2,
                            )
                        popup = pinfo.value
                    except Exception:
                        popup = None
                if popup is None:
                    try:
                        loc.first.click(timeout=15_000)
                    except Exception:
                        pass
                if popup is not None:
                    console.print("[cyan]   [DL] Popup opened — waiting for download...[/cyan]")
                    try:
                        try:
                            cdp2 = popup.context.new_cdp_session(popup)
                            cdp2.send(
                                "Browser.setDownloadBehavior",
                                {"behavior": "allow",
                                 "downloadPath": str(out_dir.resolve()),
                                 "eventsEnabled": True},
                            )
                        except Exception:
                            pass
                        popup.wait_for_timeout(5000)
                    except Exception:
                        pass

                # Watchdog: download must START within DOWNLOAD_START_TIMEOUT.
                start = time.monotonic()
                while time.monotonic() - start < DOWNLOAD_START_TIMEOUT:
                    if _download_started():
                        break
                    time.sleep(0.5)
                if not _download_started():
                    console.print(
                        f"[yellow]   [WARN] No download within {DOWNLOAD_START_TIMEOUT}s "
                        f"(attempt {attempt}/{DOWNLOAD_MAX_RETRIES}) — restarting...[/yellow]"
                    )
                    continue

                # Started — wait for it to complete (up to ~10 min).
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    dest = _download_started()
                    if dest is not None and dest.suffix != ".crdownload" and dest.stat().st_size > 0:
                        if "popup" in dir() and popup is not None:
                            try:
                                popup.close()
                            except Exception:
                                pass
                        return dest
                    time.sleep(3)
                console.print("[yellow]   [WARN] Download started but never completed.[/yellow]")
                return None
            except Exception as e:
                console.print(f"[yellow]   [WARN] '{txt}' attempt {attempt} failed: {e}[/yellow]")
                continue
    return None


def download_demo(match_id: str) -> DownloadResult:
    """Full download flow.

    Primary: FACEIT Downloads API (token-based, no browser) when
    FACEIT_DOWNLOADS_TOKEN is configured. Falls back to the browser
    scrape (authed Chrome click) if the API is unavailable.
    """
    started = datetime.now()
    match_id = _resolve_match_id(match_id)
    match_info = MatchInfo(match_id=match_id, source=DemoSource.FACEIT)
    console.print(f"\n[bold cyan][>>] FACEIT match:[/bold cyan] {match_id}")

    existing = is_already_downloaded(match_id, DemoSource.FACEIT)
    if existing:
        console.print(f"[yellow]   [SKIP] Already downloaded: {existing}[/yellow]")
        return DownloadResult(
            match=match_info, status=DownloadStatus.SKIPPED,
            demo_path=existing, file_size_mb=file_size_mb(existing),
            started_at=started, completed_at=datetime.now(),
        )

    # ── Primary: token-based Downloads API ──────────────────────────────
    if settings.has_faceit_downloads_token:
        console.print("[cyan]   [API] Trying FACEIT Downloads API...[/cyan]")
        saved = download_demo_api(match_id)
        if saved:
            return _finalize_download(match_info, saved, started)
        console.print("[yellow]   [WARN] Downloads API failed; falling back to browser scrape.[/yellow]")

    # ── Fallback: browser scrape ───────────────────────────────────────
    return _download_demo_browser(match_id, match_info, started)


def _finalize_download(match_info, saved, started) -> DownloadResult:
    """Extract the archived demo and organize it into the FACEIT demo dir."""
    from downloader import build_demo_path, record_download
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


def _download_demo_browser(match_id: str, match_info: MatchInfo, started) -> DownloadResult:
    """Fallback: open room (authed) → click demo → extract → organize."""
    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"

    try:
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
            saved = _click_demo_and_save(page, out_dir, room_url=room_url)

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
