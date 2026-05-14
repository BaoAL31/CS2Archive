"""
CS2Archive — Player Profile Image Scraper

Downloads real HLTV player body shots.
First loads the match page (to get cookies), then grabs each CDN image.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.console import Console

from config import settings

console = Console(force_terminal=True)

AVATAR_DIR = settings.demo_storage_dir / "avatars"


async def get_player_avatars(match_url: str) -> dict[str, Path]:
    """Download real HLTV player body shots."""
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
            local_path = AVATAR_DIR / f"{nickname}.jpg"
            if local_path.exists():
                result[nickname] = local_path
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

            if img_data:
                local_path.write_bytes(bytes(img_data))
                result[nickname] = local_path
                console.print(f"[green]   [OK] {nickname}.jpg ({len(img_data) / 1024:.0f} KB)[/green]")
            else:
                console.print(f"[yellow]   [{nickname}] CDN blocked[/yellow]")

        await page.close()
        return result
    finally:
        await scraper.close()
