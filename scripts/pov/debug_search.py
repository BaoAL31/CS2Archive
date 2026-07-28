import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from scrapers.player_images import CloakAvatarFetcher
import re

with CloakAvatarFetcher(headless=True) as fetcher:
    page = fetcher._new_page()
    page.goto("https://www.hltv.org/search?query=ropz", timeout=120_000)
    page.wait_for_timeout(5000)
    html = page.content()
    page.close()
    Path("search_ropz_debug.html").write_text(html, encoding="utf-8")
    print(f"Saved {len(html)} chars")

    links = re.findall(r'href="/player/(\d+)/([^"]+)"', html)
    for pid, slug in links[:10]:
        print(f"  /player/{pid}/{slug}")
    print(f"Total player links: {len(links)}")

    scripts = html.count("<script")
    print(f"Script tags: {scripts}")
    print(f"First 500 chars: {html[:500]}")