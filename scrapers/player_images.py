"""
CS2Archive — Player Profile Image Scraper

Downloads high-res HLTV player body shots (400x417) from player pages.
Resolves profile URLs via accounts → ratings → match roster, then fetches bodyshot.
Rejects images below MIN_RES (300px) and falls back to match-page capture + rembg.

Uses CloakBrowser (persistent profile + humanize) to bypass Cloudflare protection.
"""

from __future__ import annotations

import asyncio
import re
import time
from io import BytesIO
from pathlib import Path

from PIL import Image
from rich.console import Console

from config import settings
from scrapers.hltv_player_resolver import (
    avatar_cache_eligible,
    find_account_by_player_key,
    hltv_player_id_from_url,
    load_ratings_json,
    normalize_pipeline_player_key,
    resolve_from_roster,
    resolve_from_search,
    resolve_hltv_player,
)

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"
MIN_RES = 300  # Reject images smaller than 300×300

CLOAK_PROFILE = Path(".sessions/hltv-cloak")


def _hltv_avatar_path(key: str) -> Path:
    """Nested HLTV avatar path: demos/avatars/{key}/hltv/{key}.png"""
    return AVATAR_DIR / key / "hltv" / f"{key}.png"


_BODYSHOT_PLAYER_ID_RE = re.compile(r"playerbodyshot/(\d+)/", re.IGNORECASE)
_BODYSHOT_QUERY_ID_RE = re.compile(r"[?&]playerid=(\d+)", re.IGNORECASE)
_CDN_BODYSHOT_BASE = "https://img-cdn.hltv.org/playerbodyshot"

_PICK_PROFILE_BODYSHOT_JS = """
() => {
    const imgs = Array.from(document.querySelectorAll('img[src*="playerbodyshot"]'));
    if (!imgs.length) return null;
    let best = imgs[0];
    for (let i = 1; i < imgs.length; i++) {
        const area = imgs[i].naturalWidth * imgs[i].naturalHeight;
        const bestArea = best.naturalWidth * best.naturalHeight;
        if (area > bestArea) best = imgs[i];
    }
    return best.currentSrc || best.src;
}
"""

_PICK_ROSTER_JS = """
() => {
    const imgs = document.querySelectorAll('div.players img.player-photo');
    return Array.from(imgs).map(img => {
        const alt = img.alt || '';
        const m = alt.match(/'([^']+)'/);
        const nick = m ? m[1].toLowerCase() : alt.split(' ').pop().toLowerCase();
        const a = img.closest('a');
        return { nickname: nick, playerUrl: a ? a.href : null };
    });
}
"""




def _player_id_from_url(url: str | None) -> str | None:
    return hltv_player_id_from_url(url)


def _cdn_bodyshot_urls(player_id: str) -> list[str]:
    """Build direct CDN bodyshot URLs for a known HLTV player ID."""
    pid = (player_id or "").strip()
    if not pid:
        return []
    return [
        f"{_CDN_BODYSHOT_BASE}/{pid}/player.png?w=400",
        f"{_CDN_BODYSHOT_BASE}/player.png?playerid={pid}&w=400",
        f"{_CDN_BODYSHOT_BASE}/{pid}/player.png?w=300",
        f"{_CDN_BODYSHOT_BASE}/player.png?playerid={pid}&w=300",
    ]


def _bodyshot_url_matches_player(bodyshot_url: str, player_id: str) -> bool:
    """True when a CDN bodyshot URL belongs to the given HLTV player ID."""
    pid = (player_id or "").strip()
    if not pid:
        return False
    m = _BODYSHOT_PLAYER_ID_RE.search(bodyshot_url)
    if m and m.group(1) == pid:
        return True
    m = _BODYSHOT_QUERY_ID_RE.search(bodyshot_url)
    return bool(m and m.group(1) == pid)


def _cutout_bg(img_bytes: bytes) -> Image.Image:
    from rembg import remove

    return remove(Image.open(BytesIO(img_bytes)).convert("RGBA"))


def _is_acceptable(im: Image.Image) -> bool:
    if im.size[0] < MIN_RES or im.size[1] < MIN_RES:
        return False
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        extrema = alpha.getextrema()
        if extrema[0] == 0 and extrema[1] == 0:
            return False
    return True


def _upgrade_bodyshot_url(url: str, width: int = 400) -> str:
    """Request full-size HLTV bodyshot (sidebar widgets use w=100)."""
    if "w=400" in url:
        return url
    if re.search(r"[?&]w=\d+", url):
        return re.sub(r"([?&])w=\d+", rf"\1w={width}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}w={width}"


def _save_avatar_bytes_sync(raw: bytes, png_path: Path) -> bool:
    """Save bodyshot bytes to PNG, applying rembg when the source is not transparent."""
    png_path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.open(BytesIO(raw))
    if not _is_acceptable(im):
        return False

    is_transparent = im.mode == "RGBA" and min(im.getchannel("A").getextrema()) < 255
    if is_transparent:
        im.save(png_path, "PNG")
    else:
        cut = _cutout_bg(raw)
        cut.save(png_path, "PNG")

    s = png_path.stat().st_size / 1024
    console.print(
        f"[green]   [OK] {png_path.name} ({im.size[0]}x{im.size[1]}, {s:.0f} KB)[/green]"
    )
    return True


async def _save_avatar_bytes(raw: bytes, png_path: Path) -> bool:
    """Async wrapper around _save_avatar_bytes_sync."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _save_avatar_bytes_sync, raw, png_path)


class CloakAvatarFetcher:
    """Fetches HLTV player avatars using CloakBrowser to bypass Cloudflare."""

    def __init__(self, headless: bool = False):
        self._ctx = None
        self._headless = headless

    def __enter__(self):
        from cloakbrowser import launch_persistent_context

        CLOAK_PROFILE.mkdir(parents=True, exist_ok=True)
        self._ctx = launch_persistent_context(
            str(CLOAK_PROFILE.resolve()),
            headless=self._headless,
            viewport={"width": 1920, "height": 1080},
            humanize=True,
            channel="chrome",
        )
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass

    @property
    def ctx(self):
        if self._ctx is None:
            raise RuntimeError("CloakAvatarFetcher not entered as context manager")
        return self._ctx

    def _new_page(self):
        return self.ctx.new_page()

    def fetch_profile_bodyshot(self, player_url: str) -> bytes | None:
        """Profile-header bodyshot — navigates to player page, picks bodyshot img, fetches CDN."""
        page = self._new_page()
        try:
            page.goto(player_url, timeout=120_000)
            page.wait_for_timeout(5000)

            picked_src = page.evaluate(_PICK_PROFILE_BODYSHOT_JS)
            if not picked_src:
                return None

            full_url = _upgrade_bodyshot_url(picked_src)
            resp = page.goto(full_url, wait_until="domcontentloaded", timeout=60_000)
            if not resp or not resp.ok:
                return None

            raw = resp.body()
            if len(raw) < 5000 or raw.startswith(b"<html"):
                return None

            im = Image.open(BytesIO(raw))
            if not _is_acceptable(im):
                return None
            return raw
        except Exception as e:
            console.print(f"[yellow]   Profile bodyshot failed for {player_url}: {type(e).__name__}[/yellow]")
            return None
        finally:
            page.close()

    def fetch_cdn_bodyshot(self, player_id: str) -> bytes | None:
        """Fetch bodyshot directly from HLTV CDN when profile DOM interception fails."""
        pid = (player_id or "").strip()
        if not pid:
            return None

        for cdn_url in _cdn_bodyshot_urls(pid):
            page = self._new_page()
            try:
                resp = page.goto(cdn_url, wait_until="domcontentloaded", timeout=60_000)
                if not resp or not resp.ok:
                    continue
                raw = resp.body()
                if len(raw) <= 5000 or raw.startswith(b"<html"):
                    continue
                im = Image.open(BytesIO(raw))
                if _is_acceptable(im):
                    return raw
            except Exception:
                continue
            finally:
                page.close()
        return None

    def try_profile_and_cdn(self, player_url: str, player_id: str) -> bytes | None:
        """Try profile-page DOM capture, then CDN retry when player ID is known."""
        if player_url:
            raw = self.fetch_profile_bodyshot(player_url)
            if raw:
                return raw

        if player_id:
            console.print(f"[yellow]   Profile bodyshot failed; trying CDN for player {player_id}[/yellow]")
            return self.fetch_cdn_bodyshot(player_id)

        return None

    def scrape_match_roster(self, match_url: str) -> list[dict]:
        """Scrape match-page lineup for nickname → profile URL pairs."""
        page = self._new_page()
        try:
            page.goto(match_url, timeout=120_000)
            page.wait_for_timeout(3000)
            players = page.evaluate(_PICK_ROSTER_JS)
            return players or []
        except Exception as e:
            console.print(f"[yellow]   Roster scrape failed: {type(e).__name__}[/yellow]")
            return []
        finally:
            page.close()

    


def _has_hltv_identity(resolution: dict | None) -> bool:
    """True when a resolution includes an HLTV profile URL or player ID."""
    if not resolution:
        return False
    return bool(
        str(resolution.get("player_url", "") or "").strip()
        or str(resolution.get("player_id", "") or "").strip()
    )


def _promote_hltv_identity(account: object | None, resolution: dict) -> None:
    """Persist resolved HLTV profile fields on the player account after a successful fetch."""
    from player_accounts import update_hltv_player

    if account is None:
        return

    player_url = str(resolution.get("player_url", "") or "").strip()
    player_id = str(resolution.get("player_id", "") or "").strip() or (
        hltv_player_id_from_url(player_url) or ""
    )
    if not player_id or not player_url:
        return

    nickname = str(getattr(account, "nickname", "") or "")
    if not nickname:
        return

    update_hltv_player(nickname, player_id, player_url)


def _fetch_avatar_cloak(
    key: str,
    match_url: str,
    ratings_path: Path | str,
    *,
    force: bool = False,
    fetcher: CloakAvatarFetcher | None = None,
) -> Path:
    """Sync avatar fetch using CloakBrowser. Returns path to saved PNG."""
    from player_accounts import list_accounts

    key_norm = normalize_pipeline_player_key(key)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    png_path = _hltv_avatar_path(key_norm)

    accounts = list_accounts()
    account = find_account_by_player_key(accounts, key_norm)

    if not force and avatar_cache_eligible(png_path, account):
        console.print(f"[green]   [OK] {png_path.name} (cached)[/green]")
        return png_path

    ratings = load_ratings_json(ratings_path)
    resolution = resolve_hltv_player(key_norm, accounts, ratings)
    search_resolution = None
    match_url = (match_url or "").strip()

    own_fetcher = False
    if fetcher is None:
        fetcher = CloakAvatarFetcher(headless=True)
        own_fetcher = True

    try:
        if own_fetcher:
            fetcher.__enter__()

        if _has_hltv_identity(resolution):
            label = resolution.get("player_url") or f"player/{resolution.get('player_id')}"
            console.print(f"[dim]   Resolved HLTV profile via {resolution['source']}: {label}[/dim]")
            player_url = str(resolution.get("player_url", "") or "").strip()
            player_id = str(resolution.get("player_id", "") or "").strip() or (
                hltv_player_id_from_url(player_url) or ""
            )
            raw = fetcher.try_profile_and_cdn(player_url, player_id)
            if raw and _save_avatar_bytes_sync(raw, png_path):
                _promote_hltv_identity(account, resolution)
                console.print(f"[green]   Avatar resolution source: {resolution['source']}[/green]")
                return png_path
            raise RuntimeError(
                f"Failed to fetch bodyshot for {key_norm} (resolved via {resolution['source']}: "
                f"{player_url or player_id})"
            )

        if match_url:
            roster = fetcher.scrape_match_roster(match_url)
            roster_resolution = resolve_from_roster(roster, key_norm)
            if _has_hltv_identity(roster_resolution):
                label = (
                    roster_resolution.get("player_url")
                    or f"player/{roster_resolution.get('player_id')}"
                )
                console.print(f"[dim]   Resolved HLTV profile via roster: {label}[/dim]")
                player_url = str(roster_resolution.get("player_url", "") or "").strip()
                player_id = str(roster_resolution.get("player_id", "") or "").strip() or (
                    hltv_player_id_from_url(player_url) or ""
                )
                raw = fetcher.try_profile_and_cdn(player_url, player_id)
                if raw and _save_avatar_bytes_sync(raw, png_path):
                    _promote_hltv_identity(account, roster_resolution)
                    console.print("[green]   Avatar resolution source: roster[/green]")
                    return png_path

            if not _has_hltv_identity(resolution) and not _has_hltv_identity(roster_resolution):
                search_url = f"{settings.hltv_base_url}/search?query={key_norm}"
                page = fetcher._new_page()
                try:
                    page.goto(search_url, timeout=120_000)
                    page.wait_for_timeout(3000)
                    search_html = page.content()
                finally:
                    page.close()

                search_resolution = resolve_from_search(search_html, ratings, key_norm)
                if _has_hltv_identity(search_resolution):
                    label = (
                        search_resolution.get("player_url")
                        or f"player/{search_resolution.get('player_id')}"
                    )
                    console.print(f"[dim]   Resolved HLTV profile via search: {label}[/dim]")
                    player_url = str(search_resolution.get("player_url", "") or "").strip()
                    player_id = str(search_resolution.get("player_id", "") or "").strip() or (
                        hltv_player_id_from_url(player_url) or ""
                    )
                    raw = fetcher.try_profile_and_cdn(player_url, player_id)
                    if raw and _save_avatar_bytes_sync(raw, png_path):
                        _promote_hltv_identity(account, search_resolution)
                        console.print("[green]   Avatar resolution source: search[/green]")
                        return png_path
        else:
            search_url = f"{settings.hltv_base_url}/search?query={key_norm}"
            page = fetcher._new_page()
            try:
                page.goto(search_url, timeout=120_000)
                page.wait_for_timeout(3000)
                search_html = page.content()
            finally:
                page.close()

            search_resolution = resolve_from_search(search_html, ratings, key_norm)
            if _has_hltv_identity(search_resolution):
                label = (
                    search_resolution.get("player_url")
                    or f"player/{search_resolution.get('player_id')}"
                )
                console.print(f"[dim]   Resolved HLTV profile via search: {label}[/dim]")
                player_url = str(search_resolution.get("player_url", "") or "").strip()
                player_id = str(search_resolution.get("player_id", "") or "").strip() or (
                    hltv_player_id_from_url(player_url) or ""
                )
                raw = fetcher.try_profile_and_cdn(player_url, player_id)
                if raw and _save_avatar_bytes_sync(raw, png_path):
                    _promote_hltv_identity(account, search_resolution)
                    console.print("[green]   Avatar resolution source: search[/green]")
                    return png_path
            raise RuntimeError(f"no HLTV identity for {key_norm} and search returned no results")

    finally:
        if own_fetcher:
            fetcher.__exit__()

    raise RuntimeError(f"no acceptable avatar for {key_norm}")


async def fetch_avatar_for_player(
    player_key: str,
    match_url: str,
    ratings_path: Path | str,
    *,
    force: bool = False,
    scraper=None,
) -> Path:
    """
    Fetch one player avatar using accounts → ratings → roster resolution.

    Skips network fetch when cached PNG is ≥300×300 and account has hltv_player_id,
    unless force=True.

    Uses CloakBrowser to bypass Cloudflare protection on HLTV.
    The ``scraper`` parameter is accepted for backward compatibility but ignored.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _fetch_avatar_cloak, player_key, match_url, str(ratings_path), force
    )


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    """Fetch all player avatars from a match page using CloakBrowser."""
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    with CloakAvatarFetcher(headless=False) as fetcher:
        players = fetcher.scrape_match_roster(match_url)

        if not players:
            console.print("[yellow]   No player photos found[/yellow]")
            return result

        console.print(f"[dim]   Fetching {len(players)} player photos...[/dim]")

        for entry in players:
            nickname = entry["nickname"]
            png_path = _hltv_avatar_path(nickname)

            if png_path.exists():
                try:
                    im = Image.open(png_path)
                    if _is_acceptable(im):
                        result[nickname] = png_path
                        continue
                    why = "too small"
                    if im.mode == "RGBA" and im.getchannel("A").getextrema() == (0, 0):
                        why = "fully transparent (placeholder)"
                    console.print(
                        f"[yellow]   [{nickname}] existing {why} "
                        f"({im.size[0]}x{im.size[1]}), re-fetching[/yellow]"
                    )
                except Exception:
                    pass

            if not entry["playerUrl"]:
                console.print(f"[yellow]   [{nickname}] no player URL[/yellow]")
                continue

            raw = fetcher.fetch_profile_bodyshot(entry["playerUrl"])

            if not raw:
                console.print(f"[yellow]   [{nickname}] no acceptable image captured[/yellow]")
                continue

            if _save_avatar_bytes_sync(raw, png_path):
                result[nickname] = png_path

        return result
