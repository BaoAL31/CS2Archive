"""One-off: scrape HLTV results for a given date and print matches."""
from __future__ import annotations
import asyncio, sys, re
from datetime import date, timedelta
from bs4 import BeautifulSoup
from scrapers.hltv_acquire import fetch_hltv_page_html
from config import settings


async def main(target: str):
    url = f"{settings.hltv_base_url}/results?date={target}"
    print(f"[>>] Fetching {url}")
    html = await asyncio.to_thread(
        fetch_hltv_page_html, url, headless=True,
        wait_selector='a[href*="/matches/"]')
    soup = BeautifulSoup(html, "lxml")

    # date headline groups
    matches = []
    for a in soup.select('a[href*="/matches/"]'):
        href = a.get("href", "")
        m = re.search(r"/matches/(\d+)/([^/?#]+)", href)
        if not m:
            continue
        mid, slug = m.group(1), m.group(2)
        text = a.get_text(" ", strip=True)
        if not text:
            continue
        # avoid duplicate hrefs
        if any(x["mid"] == mid for x in matches):
            continue
        matches.append({"mid": mid, "slug": slug,
                        "url": settings.hltv_base_url + href, "text": text})

    print(f"[OK] {len(matches)} match link(s) found for {target}\n")
    for x in matches:
        pretty = x["slug"].replace("-vs-", " vs ").replace("-", " ").title()
        print(f"  {x['mid']}  {pretty}")
        print(f"        {x['url']}")


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    asyncio.run(main(t))
