"""
CS2Archive — FACEIT Demo Downloader (browser scrape)

No FACEIT API key required for downloads. Uses a CDP-debuggable Chrome
(``scripts/misc/launch-debug-chrome.ps1`` → ``~/.chrome-debug``, seeded with
cookies from the main logged-in Chrome profile) to open the match room, click
"Watch Demo", and capture the browser download to disk. Some matches start the
download directly from "Watch Demo"; others open a Demo 1/Demo 2 dropdown.
Debug Chrome is launched for the scrape and quit when it finishes.

Cloudflare blocks automation browsers, so the launched context uses the system
Chrome channel and reuses an authenticated profile. Pages are created in the
existing authenticated context (``browser.contexts[0]``), not ``new_page()``,
because a brand-new context doesn't inherit the logged-in cookies.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

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

    async def get_player_by_steam_id(self, steam_id: str) -> Optional[dict]:
        """FACEIT player payload for a steam64. None if not linked / not found.

        Identity is the steam64: the response is rejected unless
        ``steam_id_64`` matches the query. Nickname lookup is never used.
        """
        try:
            data = await self._request("GET", "/players", params={
                "game": "cs2", "game_player_id": steam_id,
            })
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        if str(data.get("steam_id_64") or "") != str(steam_id):
            return None
        return data

    async def get_match(self, match_id: str) -> Optional[dict]:
        """FACEIT match payload (rosters, status). None on 404."""
        try:
            return await self._request("GET", f"/matches/{match_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

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
                        # Extra keys for form aggregates (Rating-2.0 style);
                        # existing callers ignore them.
                        "assists": ps.get("Assists", "?"),
                        "kr": ps.get("K/R Ratio", "?"),
                        "double": ps.get("Double Kills", "?"),
                        "triple": ps.get("Triple Kills", "?"),
                        "quadro": ps.get("Quadro Kills", "?"),
                        "penta": ps.get("Penta Kills", "?"),
                    }
        return out

    async def get_history_items(self, player_id: str, limit: int = 30) -> list[dict]:
        """Raw /history items (results, rosters, region) for win/region math."""
        data = await self._request("GET", f"/players/{player_id}/history",
                                   params={"game": "cs2", "offset": 0, "limit": limit})
        return data.get("items", [])

    async def get_player(self, player_id: str) -> Optional[dict]:
        """Full player entity: nickname, avatar, country, cs2 elo/skill/region."""
        try:
            return await self._request("GET", f"/players/{player_id}")
        except Exception as e:
            console.print(f"[yellow]   [WARN] player lookup failed for {player_id}: {e}[/yellow]")
            return None

    async def get_player_ranking(self, player_id: str, region: str) -> Optional[int]:
        """Regional leaderboard position (1-based) or None when unavailable."""
        try:
            data = await self._request(
                "GET", f"/rankings/games/cs2/regions/{region}/players/{player_id}")
            pos = data.get("position")
            return int(pos) if pos else None
        except Exception:
            return None

    async def get_lifetime_stats(self, player_id: str) -> Optional[dict]:
        """Lifetime segment (Matches, Wins, ADR, ...) or None."""
        try:
            data = await self._request("GET", f"/players/{player_id}/stats/cs2")
            return data.get("lifetime") or None
        except Exception:
            return None

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

    def _token(self) -> str:
        # The Downloads API scope is now granted on the same Data API key.
        return settings.faceit_downloads_token or settings.faceit_api_key

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.faceit_downloads_api_base,
                headers={
                    "Authorization": f"Bearer {self._token()}",
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
        if not (settings.faceit_downloads_token or settings.faceit_api_key):
            console.print("[yellow]   [WARN] no FACEIT token set; use browser scrape.[/yellow]")
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

DOWNLOAD_BUTTON_TEXTS = ["watch demo"]

def _resolve_match_id(room_or_id: str) -> str:
    """Accept a full room URL or a bare match id."""
    if "/room/" in room_or_id:
        return room_or_id.rstrip("/").split("/room/")[-1]
    return room_or_id.strip()


def _os_downloads_dir() -> Path:
    """Windows Downloads folder (FOLDERID_Downloads), else ~/Downloads."""
    try:
        import ctypes
        import uuid

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        _fd = _GUID()
        _u = uuid.UUID("{374de290-123f-4565-9164-39c4925e467b}")
        _fd.Data1, _fd.Data2, _fd.Data3 = _u.time_low, _u.time_mid, _u.time_hi_version
        _fd.Data4 = (ctypes.c_ubyte * 8)(*_u.bytes[8:])
        _get = ctypes.windll.shell32.SHGetKnownFolderPath
        _get.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_ulong,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
        ]
        _get.restype = ctypes.c_long
        _p = ctypes.c_wchar_p()
        if _get(ctypes.byref(_fd), 0, None, ctypes.byref(_p)) == 0 and _p.value:
            d = Path(_p.value)
            try:
                ctypes.windll.ole32.CoTaskMemFree(_p)
            except Exception:
                pass
            return d
    except Exception:
        pass
    return Path.home() / "Downloads"


def _watch_dirs_for(out_dir: Optional[Path] = None) -> list[Path]:
    dirs: list[Path] = []
    for d in (out_dir, _os_downloads_dir()):
        if d is None:
            continue
        p = Path(d)
        if p not in dirs:
            dirs.append(p)
    return dirs


def _is_partial_download(path: Path) -> bool:
    n = path.name.lower()
    return (
        n.endswith(".crdownload")
        or n.endswith(".tmp")
        or n.endswith(".partial")
        or n.startswith("unconfirmed ")
    )


def _match_download_files(match_id: str, dirs: Optional[list[Path]] = None) -> list[Path]:
    hits: list[Path] = []
    if not match_id:
        return hits
    for d in dirs or _watch_dirs_for():
        if not d.is_dir():
            continue
        try:
            names = list(d.iterdir())
        except OSError:
            continue
        for p in names:
            if p.is_file() and match_id in p.name:
                hits.append(p)
    return hits


def _finished_match_archive(match_id: str, dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Largest finished demo archive for this match already on disk."""
    best: Optional[Path] = None
    best_sz = 0
    for p in _match_download_files(match_id, dirs):
        if _is_partial_download(p):
            continue
        name = p.name.lower()
        if not any(tok in name for tok in (".dem", ".zst", ".gz", ".zip", ".rar")):
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz < 1_000_000:
            continue
        if sz > best_sz:
            best, best_sz = p, sz
    return best


def _inflight_match_archive(match_id: str, dirs: Optional[list[Path]] = None) -> Optional[Path]:
    for p in _match_download_files(match_id, dirs):
        if _is_partial_download(p):
            return p
    return None


def _wait_for_match_archive(match_id: str, timeout: float = 600.0,
                            dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Wait until a finished archive exists (or an in-flight .crdownload completes)."""
    dirs = dirs or _watch_dirs_for()
    deadline = time.monotonic() + timeout
    last_log = 0.0
    saw_inflight = False
    while time.monotonic() < deadline:
        done = _finished_match_archive(match_id, dirs)
        if done:
            return done
        inflight = _inflight_match_archive(match_id, dirs)
        if inflight:
            saw_inflight = True
            if time.monotonic() - last_log >= 15:
                try:
                    mb = inflight.stat().st_size / 1e6
                except OSError:
                    mb = 0
                console.print(
                    f"  [DL] already downloading {inflight.name} ({mb:.1f} MB) — waiting..."
                )
                last_log = time.monotonic()
        elif saw_inflight:
            # Chrome renamed .crdownload → final name between polls.
            continue
        time.sleep(2)
    return _finished_match_archive(match_id, dirs)

def hold_browser_until_both(*, download_ok: bool, repeek_ok: bool) -> None:
    """Refuse to treat the room session as done until demo + Repeek both finished."""
    from scrapers.repeek_snapshot import RepeekCaptureError
    if not download_ok:
        raise RepeekCaptureError(
            "FACEIT_DOWNLOAD",
            "demo download did not finish; keeping the failure visible",
        )
    if not repeek_ok:
        raise RepeekCaptureError(
            "REPEEK_INCOMPLETE",
            "Repeek left/right capture did not finish; browser must stay up until it does",
        )


def _capture_repeek_roster(page: Any, match_id: str) -> Path:
    from scrapers.repeek_snapshot import capture_from_page, RepeekCaptureError
    try:
        page.set_viewport_size({"width": 1920, "height": 1440})
    except Exception as e:
        raise RepeekCaptureError("REPEEK_VIEWPORT", str(e)) from e
    console.print("[cyan]   [REPEEK] waiting for last-30 cards, then snapping left/right...[/cyan]")
    out = capture_from_page(page, match_id)
    console.print(f"[green]   [REPEEK] {out / 'repeek_left.png'}[/green]")
    return out


def run_download_then_repeek(
    page: Any,
    match_id: str,
    download_fn: Callable[[Any], Optional[Path]],
    close_fn: Callable[[], None],
) -> Path:
    """Download the demo, then capture Repeek. ``close_fn`` runs only afterwards."""
    saved: Optional[Path] = None
    download_ok = False
    repeek_ok = False
    try:
        saved = download_fn(page)
        download_ok = saved is not None
        if download_ok:
            _capture_repeek_roster(page, match_id)
            repeek_ok = True
        hold_browser_until_both(download_ok=download_ok, repeek_ok=repeek_ok)
        return saved  # type: ignore[return-value]
    finally:
        close_fn()


def _repeek_strips_ready(match_id: str) -> bool:
    from scrapers.repeek_snapshot import RepeekCaptureError, assert_column_pngs, strips_dir
    d = strips_dir(match_id)
    try:
        assert_column_pngs(d / "repeek_left.png", d / "repeek_right.png")
        return True
    except RepeekCaptureError:
        return False


def _ensure_repeek_roster(match_id: str) -> None:
    """Open the room only to capture left/right (demo already on disk)."""
    from scrapers.repeek_snapshot import RepeekCaptureError
    if _repeek_strips_ready(match_id):
        console.print("[yellow]   [REPEEK] left/right strips already valid — skip[/yellow]")
        return
    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"
    pw, browser = _launch_context()
    page = None
    repeek_ok = False
    try:
        page = _auth_page(browser)
        page.goto(room_url, wait_until="domcontentloaded")
        console.print("[cyan]   [REPEEK] room open (demo already downloaded); waiting for stats...[/cyan]")
        _capture_repeek_roster(page, match_id)
        repeek_ok = True
        hold_browser_until_both(download_ok=True, repeek_ok=repeek_ok)
    finally:
        _disconnect_cdp(pw, page, browser)
    if not repeek_ok:
        raise RepeekCaptureError("REPEEK_INCOMPLETE", "Repeek capture did not finish")


def _launch_context():
    """Launch the authenticated Chrome context via CDP debug Chrome.

    Runs ``scripts/misc/launch-debug-chrome.ps1`` which seeds cookies from the
    main logged-in Chrome profile (``Profile 2``) into ``~/.chrome-debug`` and
    launches a CDP-debuggable Chrome on port 9221. We then connect over CDP so
    the FACEIT room renders authenticated (Watch Demo button present) using the
    main profile's live session — no separate FACEIT login needed.

    Clicks are humanized via ``_human_click`` (jittered mouse path + delays) to
    avoid FACEIT bot detection.
    """
    from playwright.sync_api import sync_playwright

    _ensure_cdp_chrome()

    pw = sync_playwright().start()
    browser = None
    for _ in range(20):
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
            browser.contexts  # verify alive
            break
        except Exception:
            time.sleep(1)
    if browser is None:
        pw.stop()
        raise RuntimeError("Could not connect to debug Chrome on CDP port 9221")
    return pw, browser


def _kill_debug_chrome() -> None:
    """Quit the ~/.chrome-debug instance only (not the user's main Chrome)."""
    import subprocess
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" |"
            " Where-Object { $_.CommandLine -match 'chrome-debug' } |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
        ],
        timeout=30,
        capture_output=True,
    )


def _disconnect_cdp(pw, page=None, browser=None) -> None:
    """Close the room tab and quit debug Chrome when the download/scrape is done."""
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    try:
        pw.stop()
    except Exception:
        pass
    _kill_debug_chrome()


def _auth_page(browser) -> Any:
    """Open a new tab in the existing authenticated context.

    ``browser.new_page()`` on a CDP-connected Chrome creates a page in a brand-new
    context that does NOT share the logged-in profile's cookies — it hits the
    FACEIT login wall. Pages must be created in ``browser.contexts[0]`` (the
    seeded, authenticated context from launch-debug-chrome.ps1).

    Always open a new tab instead of hijacking ``pages[0]`` (that tab may be a
    chrome:// page or a room the user is looking at).
    """
    if not browser.contexts:
        return browser.new_page()
    return browser.contexts[0].new_page()


def _login_button(page):
    try:
        return page.get_by_role("button", name=re.compile(r"^log in$", re.I))
    except Exception:
        return None


def _is_authenticated(page) -> bool:
    """True if the FACEIT page is not showing the Log in wall."""
    loc = _login_button(page)
    try:
        if loc is not None and loc.count() > 0:
            box = loc.first.bounding_box()
            if box and box.get("width", 0) > 0:
                return False
    except Exception:
        pass
    return True


_CDP_PORT = int(__import__("os").environ.get("FACEIT_CDP_PORT", "9221"))


def _ensure_cdp_chrome() -> None:
    """Launch the CDP debug Chrome (idempotent), detached so it survives.

    Reuses an already-running debug Chrome on port 9221 if present. Otherwise
    runs ``launch-debug-chrome.ps1``, which seeds cookies from the main logged-in
    Chrome profile into ``~/.chrome-debug`` and starts Chrome (detached) on port
    9221. Launching detached (not tied to this process) means the debug Chrome
    stays up across runs, so subsequent downloads reconnect instantly instead of
    relaunching a fresh browser.
    """
    import subprocess
    import time

    if _port_open(_CDP_PORT):
        return
    ps1 = Path(__file__).resolve().parent.parent / "scripts" / "misc" / "launch-debug-chrome.ps1"
    console.print(f"[cyan]   [CDP] Launching debug Chrome (seed cookies from main profile)...[/cyan]")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, text=True, timeout=90,
        )
    except Exception as e:
        console.print(f"[yellow]   [CDP] launch-debug-chrome.ps1 failed: {e}[/yellow]")
    # Wait for the CDP endpoint to come up.
    for _ in range(30):
        if _port_open(_CDP_PORT):
            return
        time.sleep(1)
    console.print("[red]   [CDP] Debug Chrome did not open CDP port 9221[/red]")


def _port_open(port: int) -> bool:
    """True if something is listening on localhost:port."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1)
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()

def get_match_details(match_id: str) -> MatchInfo:
    """Scrape the room page for team names, map, and score (no API key)."""
    match_id = _resolve_match_id(match_id)
    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"

    pw, browser = _launch_context()
    page = None
    try:
        page = _auth_page(browser)
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
        _disconnect_cdp(pw, page, browser)

DOWNLOAD_START_TIMEOUT = 120  # seconds to wait for the download to begin
# (FACEIT's demo server can be slow to start — allow up to 2 min)
DOWNLOAD_MAX_RETRIES = 3     # restart attempts if the download doesn't start

def _find_demo_button(page):
    """Visible 'Watch Demo' control only. Hidden/empty matches are ignored."""
    name = re.compile(r"watch\s*demo", re.I)
    strategies = [
        lambda: page.get_by_role("button", name=name),
        lambda: page.get_by_role("link", name=name),
        lambda: page.get_by_text(name),
    ]
    for strat in strategies:
        try:
            loc = strat()
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                box = el.bounding_box()
                if not box or box["width"] < 8 or box["height"] < 8:
                    continue
                txt = (el.inner_text() or "").strip().lower()
                if "watch demo" not in txt:
                    continue
                return el
            except Exception:
                continue
    return None

def _human_click(page, x: float, y: float, variance: float = 3.0) -> None:
    """Humanized real mouse click at (x, y).

    Moves the pointer to a point near the target, then to the target in a few
    small jittered steps with human-like delays, then clicks. FACEIT's bot
    detection flags teleport-to-target + instant clicks; this gives the pointer
    a natural path so the interaction reads as human. Delays are short and
    randomized so it stays fast enough for a download scrape.
    """
    import random
    import time as _t

    # start slightly off-target, then converge in small steps
    sx = x + random.uniform(-8, 8)
    sy = y + random.uniform(-8, 8)
    page.mouse.move(sx, sy)
    _t.sleep(random.uniform(0.08, 0.25))
    steps = random.randint(2, 4)
    for i in range(1, steps + 1):
        tx = x + random.uniform(-variance, variance)
        ty = y + random.uniform(-variance, variance)
        page.mouse.move(tx, ty, steps=random.randint(3, 6))
        _t.sleep(random.uniform(0.04, 0.12))
    page.mouse.move(x, y, steps=random.randint(2, 4))
    _t.sleep(random.uniform(0.1, 0.3))
    page.mouse.down()
    _t.sleep(random.uniform(0.03, 0.09))
    page.mouse.up()
    _t.sleep(random.uniform(0.05, 0.15))

def _reload(page, room_url: Optional[str]) -> bool:
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

def _click_demo_and_save(page, out_dir: Path, room_url: Optional[str] = None,
                         match_id: str = "") -> Optional[Path]:
    """Click Watch Demo -> dropdown -> a demo option, then wait for the download.

    Expected FACEIT behavior:
      * "Watch Demo" opens an inline dropdown with "Demo 1" / "Demo 2" (one per
        map in a multi-map match). If it doesn't drop down, click it again.
      * Clicking a demo option opens a blank page for 1-2s, then the download
        starts (the CDN response routes to the browser download, landing in the
        OS default Downloads folder). The blank page is the signal a download is
        imminent.
      * After click the button often becomes a spinner for a while before the
        file appears — that is in-flight, not a miss. Do not click again.
      * If this match already has a .crdownload / finished archive on disk,
        wait for that file instead of starting a second download.
    """
    if not match_id and room_url:
        match_id = _resolve_match_id(room_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(out_dir.resolve()), "eventsEnabled": True},
        )
    except Exception as e:
        console.print(f"[yellow]   [WARN] CDP download setup failed: {e}[/yellow]")

    # The download lands in the OS default Downloads folder (the CDN request
    # bypasses page-level CDP routing). Watch BOTH out_dir and the OS Downloads
    # folder so a download is never missed (and never double-clicked).
    watch_dirs = _watch_dirs_for(out_dir)

    already = _finished_match_archive(match_id, watch_dirs)
    if already:
        console.print(f"[yellow]   [SKIP] Archive already on disk: {already.name}[/yellow]")
        return already
    inflight = _inflight_match_archive(match_id, watch_dirs)
    if inflight:
        console.print(
            f"[yellow]   [SKIP] Download already in progress: {inflight.name}[/yellow]"
        )
        return _wait_for_match_archive(match_id, timeout=600.0, dirs=watch_dirs)

    def _snapshot() -> dict[Path, int]:
        snap: dict[Path, int] = {}
        for d in watch_dirs:
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file():
                    snap[p] = p.stat().st_size
        return snap

    before = _snapshot()

    def _new_files() -> list[Path]:
        return [p for p, _ in _snapshot().items() if p not in before]

    def _download_started() -> Optional[Path]:
        """In-flight or finished archive for this match, or any new file."""
        done = _finished_match_archive(match_id, watch_dirs)
        if done:
            return done
        inflight = _inflight_match_archive(match_id, watch_dirs)
        if inflight:
            return inflight
        files = _new_files()
        if not files:
            return None
        done_new = [p for p in files if not _is_partial_download(p)]
        return (done_new or files)[0]

    def _wait_for_download(timeout: float = 600.0) -> Optional[Path]:
        """Wait until a download completes in a watched dir; return its path."""
        deadline = time.monotonic() + timeout
        last_log = time.monotonic()
        while time.monotonic() < deadline:
            done = _finished_match_archive(match_id, watch_dirs)
            if done:
                return done
            dest = _download_started()
            if dest is not None and not _is_partial_download(dest) and dest.stat().st_size > 0:
                return dest
            if time.monotonic() - last_log >= 15:
                inflight = _inflight_match_archive(match_id, watch_dirs)
                console.print(
                    f"  [DL] waiting for download to complete "
                    f"(inflight={inflight.name if inflight else '-'}, "
                    f"{len(_new_files())} new file(s))..."
                )
                last_log = time.monotonic()
            time.sleep(2)
        return _finished_match_archive(match_id, watch_dirs)

    def _demo_spinner() -> bool:
        """True if the visible Watch Demo label is gone (FACEIT preparing the file)."""
        return _find_demo_button(page) is None

    def _open_dropdown(attempt: int) -> str:
        """Click 'Watch Demo' once; return 'dropdown', 'direct', or ''.

        Some matches (single-demo, or no multi-map dropdown) start the download
        immediately when 'Watch Demo' is clicked. Others open a dropdown with
        'Demo 1'/'Demo 2' to pick from. A spinner after click means the demo is
        already being fetched — do not click again.
        """
        # Only skip the click if a file for this match is already on disk.
        # Do not treat a stale/hidden locator as a spinner — that skips the
        # click and sits on the room until the subprocess times out.
        if _finished_match_archive(match_id, watch_dirs) or _inflight_match_archive(match_id, watch_dirs):
            return "direct"
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
        loc = _find_demo_button(page)
        if loc is None:
            console.print("[yellow]   [DL] Watch Demo not visible yet[/yellow]")
            return ""
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        bbox = loc.bounding_box()
        if not bbox:
            return ""
        console.print(f"[cyan]   [DL] Clicking 'Watch Demo' (attempt {attempt})...[/cyan]")

        def _click_watch() -> None:
            try:
                loc.click(timeout=4000, delay=60)
            except Exception as e:
                console.print(f"[yellow]   [DL] locator.click failed ({e}); mouse fallback[/yellow]")
                box = loc.bounding_box() or bbox
                _human_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        def _started() -> str:
            if page.locator("text=Demo 1").count() > 0 or page.locator("text=Demo 2").count() > 0:
                return "dropdown"
            if _download_started() is not None:
                return "direct"
            extras = [pg for pg in page.context.pages[page_count_before:] if pg != page]
            if extras:
                return "direct"
            try:
                txt = (loc.inner_text() or "").strip().lower()
                if "watch demo" not in txt:
                    return "direct"
            except Exception:
                return "direct"
            return ""

        _click_watch()
        for _ in range(8):
            page.wait_for_timeout(500)
            state = _started()
            if state:
                return state
        # FACEIT often swallows the first click (focus only). Click again.
        console.print("[cyan]   [DL] No download yet — clicking Watch Demo again[/cyan]")
        _click_watch()
        for _ in range(20):
            page.wait_for_timeout(500)
            state = _started()
            if state:
                return state
        console.print("[yellow]   [DL] Click did not start a download — will retry[/yellow]")
        return ""

    page_count_before = len(page.context.pages)

    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            dropdown_state = _open_dropdown(attempt)
            if dropdown_state == "":
                if _download_started() is not None:
                    dropdown_state = "direct"
                else:
                    console.print(f"[yellow]   [DL] Could not open demo dropdown (attempt {attempt}/{DOWNLOAD_MAX_RETRIES}) — restarting...[/yellow]")
                    if not _reload(page, room_url):
                        return None
                    continue

            blank_page: Optional[Any] = None
            if dropdown_state == "dropdown":
                # Click the first demo option (Demo 1 preferred, else Demo 2).
                demo_opt = page.get_by_text("Demo 1", exact=True).first
                if demo_opt.count() == 0:
                    demo_opt = page.get_by_text("Demo 2", exact=True).first
                if demo_opt.count() == 0:
                    console.print("[yellow]   [DL] Dropdown open but no demo option found — restarting...[/yellow]")
                    if not _reload(page, room_url):
                        return None
                    continue

                console.print(f"[cyan]   [DL] Clicking '{demo_opt.inner_text().strip()}' (attempt {attempt})...[/cyan]")
                bb = demo_opt.bounding_box()
                _human_click(page, bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
            else:
                # Direct download already started from 'Watch Demo' — wait for it.
                console.print(f"[cyan]   [DL] Demo started directly from 'Watch Demo' (attempt {attempt})...[/cyan]")

            # Wait for the blank page to open (the download trigger). This can
            # take up to ~2 min for FACEIT to spin up the CDN link.
            start = time.monotonic()
            while time.monotonic() - start < DOWNLOAD_START_TIMEOUT:
                new_pages = [pg for pg in page.context.pages[page_count_before:]
                             if pg != page]
                if new_pages:
                    blank_page = new_pages[0]
                    console.print(
                        f"[cyan]   [DL] Blank page opened: '{blank_page.url[:60]}' — download should start...[/cyan]"
                    )
                    # Instrument the popup so we can see what it's doing if the
                    # download is slow or never starts: log its navigation, final
                    # response headers (status / content-type / content-disposition),
                    # console messages, and page errors.
                    try:
                        blank_page.on("framenavigated", lambda f: console.print(
                            f"  [popup nav] {f.url[:100]}" if f.url and not f.url.startswith("about:") else ""
                        ))
                        def _popup_response(r):
                            h = r.headers
                            console.print(
                                f"  [popup resp] {r.status} {r.url[:100]} "
                                f"type={h.get('content-type','?')} "
                                f"disp={h.get('content-disposition','-')}"
                            )
                        blank_page.on("response", _popup_response)
                        blank_page.on("console", lambda m: console.print(
                            f"  [popup console] {m.type}: {m.text[:120]}"
                        ))
                        blank_page.on("pageerror", lambda e: console.print(
                            f"  [popup pageerror] {e}"
                        ))
                    except Exception as _pe:
                        console.print(f"  [popup] instrument failed: {_pe}")
                    break
                if _download_started():
                    break
                time.sleep(1)

            # Wait for the download to appear and complete. The popup can sit
            # open for a while before the CDN responds — don't give up early.
            dest = _wait_for_download(timeout=600.0)
            if dest is not None:
                if blank_page is not None:
                    try:
                        blank_page.close()
                    except Exception:
                        pass
                return dest

            if _inflight_match_archive(match_id, watch_dirs):
                console.print(
                    "[yellow]   [DL] Download still in progress — not reloading.[/yellow]"
                )
                dest = _wait_for_download(timeout=600.0)
                if dest is not None:
                    return dest

            console.print(
                f"[yellow]   [WARN] No download within {DOWNLOAD_START_TIMEOUT}s "
                f"(attempt {attempt}/{DOWNLOAD_MAX_RETRIES}) — restarting...[/yellow]"
            )
            if not _reload(page, room_url):
                return None
        except Exception as e:
            console.print(f"[yellow]   [DL] attempt {attempt} failed: {e}[/yellow]")
            if not _reload(page, room_url):
                return None
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
        try:
            _ensure_repeek_roster(match_id)
        except Exception as e:
            console.print(f"[bold red]   [ERR] {e}[/bold red]")
            return DownloadResult(
                match=match_info, status=DownloadStatus.FAILED,
                demo_path=existing, error=str(e),
                started_at=started, completed_at=datetime.now(),
            )
        return DownloadResult(
            match=match_info, status=DownloadStatus.SKIPPED,
            demo_path=existing, file_size_mb=file_size_mb(existing),
            started_at=started, completed_at=datetime.now(),
        )

    # ── Primary: token-based Downloads API ──────────────────────────────
    # Only attempt the Downloads API when a real FACEIT_DOWNLOADS_TOKEN is set.
    # Falling back to the Data API key returns a 500 on every call (no Downloads
    # scope), which just adds noise + delay before the browser scrape.
    if settings.faceit_downloads_token:
        console.print("[cyan]   [API] Trying FACEIT Downloads API...[/cyan]")
        saved = download_demo_api(match_id)
        if saved:
            result = _finalize_download(match_info, saved, started)
            try:
                _ensure_repeek_roster(match_id)
            except Exception as e:
                console.print(f"[bold red]   [ERR] {e}[/bold red]")
                return DownloadResult(
                    match=match_info, status=DownloadStatus.FAILED,
                    demo_path=result.demo_path, error=str(e),
                    started_at=started, completed_at=datetime.now(),
                )
            return result
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

    # The browser download lands in the OS default Downloads folder (FACEIT's
    # CDN request bypasses CDP routing). After we've moved the .dem into the
    # project's demos/ dir, clean up any leftover downloaded archive there so it
    # isn't left behind in the user's Downloads folder.
    try:
        dl_dir = _os_downloads_dir()
        for p in dl_dir.iterdir():
            if p.is_file() and match_info.match_id in p.name and p.name != organized.name:
                p.unlink(missing_ok=True)
                console.print(f"[cyan]   [CLEAN] removed {dl_dir.name}/{p.name}[/cyan]")
    except Exception:
        pass

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
    leftover = _finished_match_archive(match_id)
    if leftover:
        console.print(f"[yellow]   [SKIP] Archive already on disk: {leftover}[/yellow]")
        result = _finalize_download(match_info, leftover, started)
        try:
            _ensure_repeek_roster(match_id)
        except Exception as e:
            console.print(f"[bold red]   [ERR] {e}[/bold red]")
            return DownloadResult(
                match=match_info, status=DownloadStatus.FAILED,
                demo_path=result.demo_path, error=str(e),
                started_at=started, completed_at=datetime.now(),
            )
        return result
    inflight = _inflight_match_archive(match_id)
    if inflight:
        console.print(f"[yellow]   [SKIP] Download already in progress: {inflight.name}[/yellow]")
        done = _wait_for_match_archive(match_id, timeout=600.0)
        if done:
            result = _finalize_download(match_info, done, started)
            try:
                _ensure_repeek_roster(match_id)
            except Exception as e:
                console.print(f"[bold red]   [ERR] {e}[/bold red]")
                return DownloadResult(
                    match=match_info, status=DownloadStatus.FAILED,
                    demo_path=result.demo_path, error=str(e),
                    started_at=started, completed_at=datetime.now(),
                )
            return result

    room_url = f"https://www.faceit.com/en/cs2/room/{match_id}"

    try:
        pw, browser = _launch_context()
        page = None
        try:
            page = _auth_page(browser)
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
            # Room is an SPA; Watch Demo appears after match payload, not at DOMContentLoaded.
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if _find_demo_button(page) is not None:
                    break
                page.wait_for_timeout(500)

            # Auth check: the Watch Demo button only renders when logged in. If
            # the debug Chrome isn't authenticated, give clear guidance instead
            # of silently failing all download attempts.
            if not _is_authenticated(page):
                console.print(
                    "[red]   [AUTH] FACEIT is not logged in — the Watch Demo button is hidden.[/red]"
                )
                console.print(
                    "[yellow]   Fix: run `scripts/misc/launch-debug-chrome.ps1`, log into FACEIT in the "
                    "opened Chrome, then retry.[/yellow]"
                )
                return DownloadResult(
                    match=match_info, status=DownloadStatus.FAILED,
                    started_at=started, completed_at=datetime.now(),
                )

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
            saved = _click_demo_and_save(page, out_dir, room_url=room_url, match_id=match_id)

            if not saved:
                console.print("[red]   [ERR] Could not find a demo download button.[red]")
                return DownloadResult(
                    match=match_info, status=DownloadStatus.FAILED,
                    error="No demo download button found",
                    started_at=started, completed_at=datetime.now(),
                )

            console.print(f"[green]   [OK] Downloaded: {saved.name} ({file_size_mb(saved):.1f} MB)[/green]")
            # Same tab: Repeek has been loading during the transfer. Do not
            # close until last-30 cards are snapped.
            _capture_repeek_roster(page, match_id)
            hold_browser_until_both(download_ok=True, repeek_ok=True)

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
            _disconnect_cdp(pw, page, browser)

    except Exception as e:
        console.print(f"[bold red]   [ERR] Error: {e}[/bold red]")
        return DownloadResult(
            match=match_info, status=DownloadStatus.FAILED,
            error=str(e), started_at=started, completed_at=datetime.now(),
        )
