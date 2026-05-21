"""
CS2Archive — Player Profile Image Scraper

Downloads high-res HLTV player body shots (400x417) from player pages.
Rejects images below MIN_RES (300px) and falls back to match-page capture + rembg.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image
from rembg import remove
from rich.console import Console

from config import settings

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"
MIN_RES = 300  # Reject images smaller than 300×300


def _cutout_bg(img_bytes: bytes) -> Image.Image:
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


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    from scrapers.hltv import HLTVScraper

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    scraper = HLTVScraper()
    try:
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

        if not players:
            console.print("[yellow]   No player photos found[/yellow]")
            return result

        console.print(f"[dim]   Fetching {len(players)} player photos...[/dim]")
        loop = asyncio.get_event_loop()

        for entry in players:
            nickname = entry["nickname"]
            png_path = AVATAR_DIR / f"{nickname}.png"

            if png_path.exists():
                try:
                    im = Image.open(png_path)
                    if _is_acceptable(im):
                        result[nickname] = png_path
                        continue
                    else:
                        console.print(f"[yellow]   [{nickname}] existing too small ({im.size[0]}x{im.size[1]}), re-fetching[/yellow]")
                except Exception:
                    pass

            if not entry["playerUrl"]:
                console.print(f"[yellow]   [{nickname}] no player URL[/yellow]")
                continue

            raw = await _try_sizes(scraper, entry["playerUrl"])

            if not raw:
                console.print(f"[yellow]   [{nickname}] no acceptable image captured[/yellow]")
                continue

            im = Image.open(BytesIO(raw))
            is_transparent = im.mode == "RGBA" and min(im.getchannel("A").getextrema()) < 255

            if is_transparent:
                im.save(png_path, "PNG")
                s = png_path.stat().st_size / 1024
                console.print(f"[green]   [OK] {nickname}.png ({im.size[0]}x{im.size[1]}, {s:.0f} KB)[/green]")
                result[nickname] = png_path
            else:
                cut = await loop.run_in_executor(None, _cutout_bg, raw)
                cut.save(png_path, "PNG")
                s = png_path.stat().st_size / 1024
                console.print(f"[green]   [OK] {nickname}.png ({im.size[0]}x{im.size[1]}, {s:.0f} KB)[/green]")
                result[nickname] = png_path

        return result
    finally:
        await scraper.close()
