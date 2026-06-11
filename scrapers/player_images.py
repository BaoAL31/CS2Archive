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
    resolve_hltv_player,
)

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"
MIN_RES = 300  # Reject images smaller than 300×300

_BODYSHOT_PLAYER_ID_RE = re.compile(r"playerbodyshot/(\d+)/", re.IGNORECASE)
_BODYSHOT_QUERY_ID_RE = re.compile(r"[?&]playerid=(\d+)", re.IGNORECASE)


def _player_id_from_url(url: str | None) -> str | None:
    return hltv_player_id_from_url(url)


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


async def _capture_from_url(ctx, url: str) -> dict[str, bytes]:
    """Navigate to a page and capture playerbodyshot responses."""
    cap_page = await ctx.new_page()
    captured: dict[str, bytes] = {}

    async def on_resp(resp):
        if "playerbodyshot" not in resp.url:
            return
        if captured:
            return
        try:
            body = await resp.body()
            if len(body) > 5000 and not body.startswith(b"<html"):
                captured[resp.url] = body
        except Exception:
            pass

    cap_page.on("response", on_resp)
    try:
        await cap_page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)
    except Exception:
        await asyncio.sleep(3)
    await cap_page.close()
    return captured


async def _try_sizes(scraper, player_url: str) -> bytes | None:
    """Try w=400, then w=300, then any size — return first acceptable image."""
    for size_param in ["w=400", "w=300", ""]:
        cap_ctx = await scraper.fresh_context()
        cap_page = await cap_ctx.new_page()
        captured: dict[str, bytes] = {}

        async def on_resp(resp):
            if "playerbodyshot" not in resp.url:
                return
            if size_param and size_param not in resp.url:
                return
            if captured:
                return
            try:
                body = await resp.body()
                if len(body) > 5000 and not body.startswith(b"<html"):
                    captured[resp.url] = body
            except Exception:
                pass

        cap_page.on("response", on_resp)
        try:
            await cap_page.goto(player_url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
        except Exception:
            await asyncio.sleep(3)
        await cap_page.close()
        await cap_ctx.close()

        if captured:
            raw = max(captured.values(), key=lambda b: len(b))
            try:
                im = Image.open(BytesIO(raw))
                if _is_acceptable(im):
                    return raw
            except Exception:
                pass

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
    """Last resort: download match-page lineup photo for one player."""
    context = await scraper._ensure_browser()
    page = await context.new_page()
    await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    img_url = await page.evaluate(
        """
        (nick) => {
            const imgs = document.querySelectorAll('div.players img.player-photo');
            for (const img of imgs) {
                const alt = img.alt || '';
                const m = alt.match(/'([^']+)'/);
                const n = m ? m[1].toLowerCase() : alt.split(' ').pop().toLowerCase();
                if (n === nick) return img.src || null;
            }
            return null;
        }
        """,
        player_key,
    )
    await page.close()

    if not img_url:
        return None

    cap_ctx = await scraper.fresh_context()
    cap_page = await cap_ctx.new_page()
    try:
        resp = await cap_page.goto(img_url, wait_until="domcontentloaded", timeout=20000)
        if resp is None or not resp.ok:
            return None
        raw = await resp.body()
        if len(raw) <= 5000 or raw.startswith(b"<html"):
            return None
        im = Image.open(BytesIO(raw))
        if _is_acceptable(im):
            return raw
    except Exception:
        return None
    finally:
        await cap_page.close()
        await cap_ctx.close()
    return None


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
    try:
        if resolution and resolution.get("player_url"):
            console.print(
                f"[dim]   Resolved HLTV profile via {resolution['source']}: "
                f"{resolution['player_url']}[/dim]"
            )
            raw = await _try_sizes(scraper, resolution["player_url"])
            if raw and await _save_avatar_bytes(raw, png_path):
                _promote_hltv_identity(account, resolution)
                console.print(
                    f"[green]   Avatar resolution source: {resolution['source']}[/green]"
                )
                return png_path

        roster = await _scrape_match_roster(scraper, match_url)
        roster_resolution = resolve_from_roster(roster, key)
        if roster_resolution and roster_resolution.get("player_url"):
            console.print(
                f"[dim]   Resolved HLTV profile via roster: "
                f"{roster_resolution['player_url']}[/dim]"
            )
            raw = await _try_sizes(scraper, roster_resolution["player_url"])
            if raw and await _save_avatar_bytes(raw, png_path):
                _promote_hltv_identity(account, roster_resolution)
                console.print("[green]   Avatar resolution source: roster[/green]")
                return png_path

        console.print("[yellow]   Profile bodyshot failed; trying match-page fallback[/yellow]")
        raw = await _fetch_match_page_headshot(scraper, match_url, key)
        if raw and await _save_avatar_bytes(raw, png_path):
            promote_resolution = roster_resolution or resolution
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

            raw = await _try_sizes(scraper, entry["playerUrl"])

            if not raw:
                console.print(f"[yellow]   [{nickname}] no acceptable image captured[/yellow]")
                continue

            if await _save_avatar_bytes(raw, png_path):
                result[nickname] = png_path

        return result
    finally:
        await scraper.close()
