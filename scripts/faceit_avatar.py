"""
CS2Archive — FACEIT avatar scraper.

For FACEIT players (no HLTV pro bodyshot), grab the top N Google Images
results for a nickname, run each through rembg background removal, pick the
best cutout, and save as demos/avatars/{nick}.png (replacing the HLTV-style
cutout path so the thumbnail generator picks it up transparently).

Usage:
    python scripts/faceit_avatar.py donk [--top 3] [--force]
    python scripts/faceit_avatar.py "m0NESY" --top 5
"""

from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
from PIL import Image
from rich.console import Console

try:
    from playwright.async_api import async_playwright
except ImportError:  # fallback for sync envs
    async_playwright = None  # type: ignore

console = Console(force_terminal=True)

AVATAR_DIR = Path("demos/avatars")
MIN_RES = 200  # Full-res Bing source images; reject anything smaller
TOP_DEFAULT = 3
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _cutout(raw: bytes) -> Image.Image:
    from rembg import remove

    return remove(Image.open(BytesIO(raw)).convert("RGBA"))


def _acceptable(im: Image.Image) -> bool:
    if im.size[0] < MIN_RES or im.size[1] < MIN_RES:
        return False
    if im.mode == "RGBA":
        extrema = im.getchannel("A").getextrema()
        if extrema[0] == 0 and extrema[1] == 0:
            return False
    return True


async def _collect_image_urls(nick: str, top: int) -> list[str]:
    """Collect candidate image URLs from Bing Images for `nick`.

    Bing is used instead of Google (Google serves a consent wall that blocks
    headless scraping). We pull the thumbnail <img> srcs which are real JPEGs
    (~150-300px) — fine for a small FACEIT avatar after rembg cutout.
    """
    query = f"{nick} cs2 player"
    url = "https://www.bing.com/images/search?q=" + httpx.QueryParams({"q": query})["q"]

    urls: list[str] = []
    if async_playwright is None:
        console.print("[red]playwright not installed in this env[/red]")
        return urls

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, channel="chrome", args=["--no-sandbox"]
        )
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            for _ in range(3):
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(800)

            imgs = await page.query_selector_all("a.iusc")
            for a in imgs:
                href = await a.get_attribute("href") or ""
                if "mediaurl=" not in href:
                    continue
                from urllib.parse import parse_qs, urlparse

                m = parse_qs(urlparse(href).query).get("mediaurl", [""])[0]
                if m.startswith("http"):
                    urls.append(m)
                if len(urls) >= top * 4:
                    break
        except Exception as e:
            console.print(f"[yellow]  image scrape error: {type(e).__name__}: {e}[/yellow]")
        finally:
            await browser.close()

    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique[: top * 3]


def _center_subject(im: Image.Image) -> Image.Image:
    """Crop to the opaque subject and re-center it on a square canvas.

    Removes left/right bias (e.g. donk sitting off-center) by finding the
    alpha bounding box and pasting the subject centered on a square whose
    side is the longer of width/height.
    """
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    alpha = im.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return im
    subject = im.crop(bbox)
    w, h = subject.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ox = (side - w) // 2
    oy = (side - h) // 2
    canvas.paste(subject, (ox, oy), subject)
    return canvas


def _is_clean(im: Image.Image) -> bool:
    """Heuristic: reject cutouts containing stray objects (chair, desk, etc.).

    A clean single-subject avatar has:
      - one connected opaque blob (person not fused to furniture),
      - bbox aspect width/height <= 1.15 (a person is taller than wide;
        a chair beside them makes it wide),
      - vertical centroid near middle (vcenter in [0.40, 0.62]); a chair
        at the bottom drags the subject downward.
    """
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    try:
        from scipy import ndimage
    except Exception:
        return True  # can't analyze; accept
    a = np.array(im.getchannel("A")) > 30
    if a.sum() == 0:
        return False
    lab, n = ndimage.label(a)
    if n > 1:
        return False
    ys, xs = np.where(a)
    bb_w = xs.max() - xs.min()
    bb_h = ys.max() - ys.min()
    if bb_h == 0:
        return False
    aspect = bb_w / bb_h
    vcenter = ys.mean() / im.size[1]
    if aspect > 1.15:
        return False
    if not (0.40 <= vcenter <= 0.62):
        return False
    return True


async def _fetch_and_cutout(url: str, timeout: float = 20.0) -> Image.Image | None:
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _UA}, follow_redirects=True, timeout=timeout
        ) as client:
            resp = await client.get(url)
        raw = resp.content
        if len(raw) < 2000:
            return None
        im = Image.open(BytesIO(raw))
        if not _acceptable(im):
            return None
        cut = await asyncio.get_event_loop().run_in_executor(None, _cutout, raw)
        cut = _center_subject(cut)
        return cut
    except Exception:
        return None


async def fetch_faceit_avatar(nick: str, *, top: int = TOP_DEFAULT, force: bool = False) -> list[Path]:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    key = nick.strip().lower()
    out_dir = AVATAR_DIR / key / "faceit"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Existing variants (donk.png, donk_2.png, ...) unless --force
    existing = sorted(out_dir.glob(f"{key}*.png"))
    if existing and not force:
        console.print(
            f"[green]  [OK] {len(existing)} faceit avatar(s) already in {out_dir}[/green]"
        )
        return existing

    console.print(f"[cyan]  Collecting top {top} Bing Images for '{nick}'...[/cyan]")
    urls = await _collect_image_urls(nick, top)
    console.print(f"[dim]  Collected {len(urls)} candidate URLs[/dim]")

    candidates: list[Image.Image] = []
    for i, u in enumerate(urls):
        cut = await _fetch_and_cutout(u)
        if cut is not None:
            candidates.append(cut)
            console.print(
                f"[green]  [OK] cutout {i+1}: {cut.size[0]}x{cut.size[1]}[/green]"
            )
        if len(candidates) >= top:
            break

    if not candidates:
        raise RuntimeError(f"no acceptable FACEIT avatar for {nick}")

    # Prefer clean (no chair/desk) cutouts, then largest by pixel area.
    def _score(im: Image.Image) -> tuple[bool, int]:
        return (_is_clean(im), im.size[0] * im.size[1])

    candidates.sort(key=_score, reverse=True)
    saved: list[Path] = []
    for idx, im in enumerate(candidates, start=1):
        name = f"{key}.png" if idx == 1 else f"{key}_{idx}.png"
        p = out_dir / name
        im.save(p, "PNG")
        s = p.stat().st_size / 1024
        console.print(
            f"[green]  Saved {p.name} ({im.size[0]}x{im.size[1]}, {s:.0f} KB)[/green]"
        )
        saved.append(p)
    return saved


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("nick")
    ap.add_argument("--top", type=int, default=TOP_DEFAULT)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    paths = asyncio.run(fetch_faceit_avatar(args.nick, top=args.top, force=args.force))
    sys.exit(0 if paths else 1)


if __name__ == "__main__":
    main()
