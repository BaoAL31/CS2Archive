"""
CS2Archive — Player Profile Image Scraper

Downloads high-res HLTV player body shots (400x417) from player pages.
Resolves profile URLs via accounts → ratings → match roster, then fetches bodyshot.
Rejects images below MIN_RES (300px) and falls back to match-page capture + rembg.
"""

from __future__ import annotations

import asyncio
import re
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

_BODYSHOT_PLAYER_ID_RE = re.compile(r"playerbodyshot/(\d+)/", re.IGNORECASE)
_BODYSHOT_QUERY_ID_RE = re.compile(r"[?&]playerid=(\d+)", re.IGNORECASE)
_CDN_BODYSHOT_BASE = "https://img-cdn.hltv.org/playerbodyshot"

_BODYSHOT_LOCATORS = (
    '.playerBodyshot img[src*="playerbodyshot"]',
    'img.bodyshot-img[src*="playerbodyshot"]',
)

_PICK_PROFILE_BODYSHOT_JS = """
([sel, expectedId]) => {
    const all = Array.from(document.querySelectorAll(sel));
    let imgs = all.filter((img) => {
        const src = (img.currentSrc || img.src || '').toLowerCase();
        return src.includes('playerbodyshot') && !src.includes('w=100') && !src.includes('h=100');
    });
    if (!imgs.length) return null;
    if (expectedId) {
        const id = String(expectedId);
        const matched = imgs.filter((img) => {
            const src = (img.currentSrc || img.src || '').toLowerCase();
            return src.includes('/playerbodyshot/' + id + '/') || src.includes('playerid=' + id);
        });
        if (matched.length) imgs = matched;
    }
    let best = imgs[0];
    for (let i = 1; i < imgs.length; i++) {
        const area = imgs[i].naturalWidth * imgs[i].naturalHeight;
        const bestArea = best.naturalWidth * best.naturalHeight;
        if (area > bestArea) best = imgs[i];
    }
    return best.currentSrc || best.src;
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
    return im.size[0] >= MIN_RES and im.size[1] >= MIN_RES


def _upgrade_bodyshot_url(url: str, width: int = 400) -> str:
    """Request full-size HLTV bodyshot (sidebar widgets use w=100)."""
    if "w=400" in url:
        return url
    if re.search(r"[?&]w=\d+", url):
        return re.sub(r"([?&])w=\d+", rf"\1w={width}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}w={width}"


async def _fetch_profile_bodyshot(scraper, player_url: str) -> bytes | None:
    """Profile-header bodyshot only — uses DOM-selected img src (hash CDN URLs)."""
    expected_id = hltv_player_id_from_url(player_url) or ""
    await scraper._ensure_browser()
    cap_ctx = await scraper.fresh_context()
    page = await cap_ctx.new_page()
    try:
        await page.goto(player_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        picked_src: str | None = None
        for sel in _BODYSHOT_LOCATORS:
            try:
                picked_src = await page.evaluate(_PICK_PROFILE_BODYSHOT_JS, [sel, expected_id])
            except Exception:
                continue
            if picked_src:
                break

        if not picked_src:
            return None

        full_url = _upgrade_bodyshot_url(picked_src)
    finally:
        await page.close()
        await cap_ctx.close()

    # CDN fetch on a fresh page — same-tab navigation from profile page returns 403/HTML.
    dl_ctx = await scraper.fresh_context()
    dl_page = await dl_ctx.new_page()
    try:
        resp = await dl_page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
        if resp is None or not resp.ok:
            return None
        raw = await resp.body()
        if len(raw) < 5000 or raw.startswith(b"<html"):
            return None

        im = Image.open(BytesIO(raw))
        if not _is_acceptable(im):
            return None
        return raw
    finally:
        await dl_page.close()
        await dl_ctx.close()


async def _fetch_cdn_bodyshot(scraper, player_id: str) -> bytes | None:
    """Fetch bodyshot directly from HLTV CDN when profile DOM interception fails."""
    await scraper._ensure_browser()
    pid = (player_id or "").strip()
    if not pid:
        return None

    for cdn_url in _cdn_bodyshot_urls(pid):
        cap_ctx = await scraper.fresh_context()
        cap_page = await cap_ctx.new_page()
        try:
            resp = await cap_page.goto(cdn_url, wait_until="domcontentloaded", timeout=20000)
            if resp is None or not resp.ok:
                continue
            raw = await resp.body()
            if len(raw) <= 5000 or raw.startswith(b"<html"):
                continue
            im = Image.open(BytesIO(raw))
            if _is_acceptable(im):
                return raw
        except Exception:
            continue
        finally:
            await cap_page.close()
            await cap_ctx.close()

    return None


async def _try_profile_and_cdn(scraper, resolution: dict) -> bytes | None:
    """Try profile-page DOM capture, then CDN retry when player ID is known."""
    player_url = str(resolution.get("player_url", "") or "").strip()
    player_id = str(resolution.get("player_id", "") or "").strip() or (
        hltv_player_id_from_url(player_url) or ""
    )

    if player_url:
        raw = await _fetch_profile_bodyshot(scraper, player_url)
        if raw:
            return raw

    if player_id:
        console.print(
            f"[yellow]   Profile bodyshot failed; trying CDN for player {player_id}[/yellow]"
        )
        return await _fetch_cdn_bodyshot(scraper, player_id)

    return None


async def _scrape_match_roster(scraper, match_url: str) -> list[dict]:
    """Scrape match-page lineup for nickname → profile URL pairs."""
    context = await scraper._ensure_browser()
    page = await context.new_page()
    await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    players = await page.evaluate("""
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
    """)
    await page.close()
    return players or []


async def _fetch_match_page_headshot(scraper, match_url: str, player_key: str) -> bytes | None:
    """Last resort: screenshot match-page lineup photo for one player."""
    context = await scraper._ensure_browser()
    page = await context.new_page()
    try:
        await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        entries = await page.evaluate(
            """
            () => {
                const imgs = document.querySelectorAll('div.players img.player-photo');
                return Array.from(imgs).map((img, index) => {
                    const alt = img.alt || '';
                    const m = alt.match(/'([^']+)'/);
                    const nick = m ? m[1].toLowerCase() : alt.split(' ').pop().toLowerCase();
                    return { nickname: nick, index };
                });
            }
            """
        )
        match = next((e for e in entries if e["nickname"] == player_key), None)
        if not match:
            console.print(f"[yellow]   [{player_key}] not found on match page for fallback[/yellow]")
            return None

        locator = page.locator("div.players img.player-photo").nth(match["index"])
        shot = await locator.screenshot(type="png")
        if not shot or len(shot) < 1000:
            return None

        im = Image.open(BytesIO(shot))
        if _is_acceptable(im):
            return shot
        console.print(
            f"[yellow]   [{player_key}] match-page photo too small ({im.size[0]}x{im.size[1]})[/yellow]"
        )
        return None
    finally:
        await page.close()


async def _save_avatar_bytes(raw: bytes, png_path: Path) -> bool:
    """Save bodyshot bytes to PNG, applying rembg when the source is not transparent."""
    loop = asyncio.get_event_loop()
    im = Image.open(BytesIO(raw))
    if not _is_acceptable(im):
        return False

    is_transparent = im.mode == "RGBA" and min(im.getchannel("A").getextrema()) < 255
    if is_transparent:
        im.save(png_path, "PNG")
    else:
        cut = await loop.run_in_executor(None, _cutout_bg, raw)
        cut.save(png_path, "PNG")

    s = png_path.stat().st_size / 1024
    console.print(
        f"[green]   [OK] {png_path.name} ({im.size[0]}x{im.size[1]}, {s:.0f} KB)[/green]"
    )
    return True


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


async def fetch_avatar_for_player(
    player_key: str,
    match_url: str,
    ratings_path: Path | str,
    *,
    force: bool = False,
) -> Path:
    """
    Fetch one player avatar using accounts → ratings → roster resolution.

    Skips network fetch when cached PNG is ≥300×300 and account has hltv_player_id,
    unless force=True.
    """
    from player_accounts import list_accounts

    key = normalize_pipeline_player_key(player_key)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    png_path = AVATAR_DIR / f"{key}.png"

    accounts = list_accounts()
    account = find_account_by_player_key(accounts, key)

    if not force and avatar_cache_eligible(png_path, account):
        console.print(f"[green]   [OK] {png_path.name} (cached)[/green]")
        return png_path

    ratings = load_ratings_json(ratings_path)
    resolution = resolve_hltv_player(key, accounts, ratings)

    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    roster_resolution = None
    search_resolution = None
    try:
        await scraper._ensure_browser()
        if _has_hltv_identity(resolution):
            label = resolution.get("player_url") or f"player/{resolution.get('player_id')}"
            console.print(
                f"[dim]   Resolved HLTV profile via {resolution['source']}: {label}[/dim]"
            )
            raw = await _try_profile_and_cdn(scraper, resolution)
            if raw and await _save_avatar_bytes(raw, png_path):
                _promote_hltv_identity(account, resolution)
                console.print(
                    f"[green]   Avatar resolution source: {resolution['source']}[/green]"
                )
                return png_path

        roster = await _scrape_match_roster(scraper, match_url)
        roster_resolution = resolve_from_roster(roster, key)
        if _has_hltv_identity(roster_resolution):
            label = (
                roster_resolution.get("player_url")
                or f"player/{roster_resolution.get('player_id')}"
            )
            console.print(f"[dim]   Resolved HLTV profile via roster: {label}[/dim]")
            raw = await _try_profile_and_cdn(scraper, roster_resolution)
            if raw and await _save_avatar_bytes(raw, png_path):
                _promote_hltv_identity(account, roster_resolution)
                console.print("[green]   Avatar resolution source: roster[/green]")
                return png_path

        if not _has_hltv_identity(resolution) and not _has_hltv_identity(
            roster_resolution
        ):
            search_url = f"{settings.hltv_base_url}/search?query={key}"
            search_html = await scraper._get_page_content(search_url)
            search_resolution = resolve_from_search(search_html, ratings, key)
            if _has_hltv_identity(search_resolution):
                label = (
                    search_resolution.get("player_url")
                    or f"player/{search_resolution.get('player_id')}"
                )
                console.print(
                    f"[dim]   Resolved HLTV profile via search: {label}[/dim]"
                )
                raw = await _try_profile_and_cdn(scraper, search_resolution)
                if raw and await _save_avatar_bytes(raw, png_path):
                    _promote_hltv_identity(account, search_resolution)
                    console.print("[green]   Avatar resolution source: search[/green]")
                    return png_path

        console.print("[yellow]   Profile bodyshot failed; trying match-page fallback[/yellow]")
        raw = await _fetch_match_page_headshot(scraper, match_url, key)
        if raw and await _save_avatar_bytes(raw, png_path):
            promote_resolution = search_resolution or roster_resolution or resolution
            if promote_resolution:
                _promote_hltv_identity(account, promote_resolution)
            console.print("[green]   Avatar resolution source: match_fallback[/green]")
            return png_path
    finally:
        await scraper.close()

    raise RuntimeError(f"no acceptable avatar for {key}")


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    from scrapers.hltv import HLTVScraper

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    scraper = HLTVScraper()
    try:
        players = await _scrape_match_roster(scraper, match_url)

        if not players:
            console.print("[yellow]   No player photos found[/yellow]")
            return result

        console.print(f"[dim]   Fetching {len(players)} player photos...[/dim]")

        for entry in players:
            nickname = entry["nickname"]
            png_path = AVATAR_DIR / f"{nickname}.png"

            if png_path.exists():
                try:
                    im = Image.open(png_path)
                    if _is_acceptable(im):
                        result[nickname] = png_path
                        continue
                    console.print(
                        f"[yellow]   [{nickname}] existing too small "
                        f"({im.size[0]}x{im.size[1]}), re-fetching[/yellow]"
                    )
                except Exception:
                    pass

            if not entry["playerUrl"]:
                console.print(f"[yellow]   [{nickname}] no player URL[/yellow]")
                continue

            raw = await _fetch_profile_bodyshot(scraper, entry["playerUrl"])

            if not raw:
                console.print(f"[yellow]   [{nickname}] no acceptable image captured[/yellow]")
                continue

            if await _save_avatar_bytes(raw, png_path):
                result[nickname] = png_path

        return result
    finally:
        await scraper.close()
