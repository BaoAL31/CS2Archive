"""
CS2Archive — Player Profile Image Scraper

Downloads real HLTV player body shots and removes their backgrounds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image
from rembg import remove
from rich.console import Console

from config import settings

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"


def _cutout_bg(jpg_path: Path, png_path: Path) -> bool:
    try:
        raw = Image.open(jpg_path).convert("RGBA")
        cut = remove(raw)
        cut.save(png_path, "PNG")
        return True
    except Exception as e:
        console.print(f"[yellow]   [{jpg_path.stem}] cutout failed: {e}[/yellow]")
        return False


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    """Download real HLTV player body shots and save cutout PNGs."""
    from scrapers.hltv import HLTVScraper

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}

    scraper = HLTVScraper()
    try:
        context = await scraper._ensure_browser()
        page = await context.new_page()

        await page.goto(match_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        body_shots = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img.player-photo');
                return Array.from(imgs).map(img => {
                    const alt = img.alt || '';
                    const m = alt.match(/'([^']+)'/);
                    const nick = m ? m[1].toLowerCase() : alt.split(' ').pop().toLowerCase();
                    return { nickname: nick, src: img.src };
                });
            }
        """)

        if not body_shots:
            console.print("[yellow]   No player photos found[/yellow]")
            await page.close()
            return result

        console.print(f"[dim]   Downloading {len(body_shots)} player photos...[/dim]")

        for entry in body_shots:
            nickname = entry["nickname"]
            src = entry["src"]
            png_path = AVATAR_DIR / f"{nickname}.png"
            jpg_path = AVATAR_DIR / f"{nickname}.jpg"

            if png_path.exists():
                result[nickname] = png_path
                continue

            img_data = await page.evaluate(f"""
                async () => {{
                    try {{
                        const resp = await fetch('{src}');
                        if (!resp.ok) return null;
                        const blob = await resp.blob();
                        if (blob.size < 10000) return null;
                        const buf = await blob.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }} catch(e) {{ return null; }}
                }}
            """)

            if not img_data:
                console.print(f"[yellow]   [{nickname}] CDN blocked[/yellow]")
                continue

            jpg_path.write_bytes(bytes(img_data))
            kb = len(img_data) / 1024
            console.print(f"[green]   [OK] {nickname}.jpg ({kb:.0f} KB)[/green]")

            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, _cutout_bg, jpg_path, png_path)
            if ok:
                console.print(f"[green]         {nickname}.png cutout ({png_path.stat().st_size / 1024:.0f} KB)[/green]")
                result[nickname] = png_path
            else:
                result[nickname] = jpg_path

        await page.close()
        return result
    finally:
        await scraper.close()
