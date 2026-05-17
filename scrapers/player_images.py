"""
CS2Archive — Player Profile Image Scraper

Downloads high-res HLTV player body shots (400x417) from player pages.
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


def _cutout_bg(img_bytes: bytes) -> Image.Image:
    return remove(Image.open(BytesIO(img_bytes)).convert("RGBA"))


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
                const imgs = document.querySelectorAll('img.player-photo');
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

            if png_path.exists() and png_path.stat().st_size > 20000:
                try:
                    im = Image.open(png_path)
                    if im.size[0] >= 200 and im.size[1] >= 200:
                        result[nickname] = png_path
                        continue
                except Exception:
                    pass

            if not entry["playerUrl"]:
                console.print(f"[yellow]   [{nickname}] no player URL[/yellow]")
                continue

            # Fresh context per player to avoid stale cache
            cap_ctx = await scraper.fresh_context()
            cap_page = await cap_ctx.new_page()
            captured: dict[str, bytes] = {}

            async def on_resp(resp):
                if "playerbodyshot" not in resp.url:
                    return
                try:
                    body = await resp.body()
                    if len(body) > 5000 and not body.startswith(b"<html"):
                        captured[resp.url] = body
                except Exception:
                    pass

            cap_page.on("response", on_resp)
            try:
                await cap_page.goto(entry["playerUrl"], wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(3)
            except Exception:
                await asyncio.sleep(3)
            await cap_page.close()
            await cap_ctx.close()

            if not captured:
                console.print(f"[yellow]   [{nickname}] no image captured[/yellow]")
                continue

            raw = max(captured.values(), key=lambda b: len(b))
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
