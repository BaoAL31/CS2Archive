import sys, asyncio
sys.path.extend(["scripts", "."])
from scrapers.hltv import HLTVScraper
import re

async def main():
    s = HLTVScraper()
    urls = {
        "m0NESY": "https://www.hltv.org/player/19230/m0nesy",
        "b1t": "https://www.hltv.org/player/20124/b1t",
    }
    for nick, u in urls.items():
        html = await s._get_page_content(u)
        print("====", nick, "len", len(html))
        # team name often in <div class="player-team"> or a team link with title
        for pat in [r'class="player-team"[^>]*>.*?title="([^"]+)"',
                    r'player-team.*?alt="([^"]+)"',
                    r'<a[^>]+href="/team/\d+/[^"]*"[^>]*>([^<]+)</a>',
                    r'"team-and-label".*?title="([^"]+)"']:
            m = re.search(pat, html, re.S)
            if m:
                print("  match", pat[:30], "->", m.group(1)[:60])
        # show window around 'Current team' or 'player-team'
        for kw in ["player-team", "Current team", "teamContainer"]:
            i = html.find(kw)
            if i > 0:
                print("  --", kw, "ctx:", re.sub(r"\s+", " ", html[i-100:i+200])[:260])
                break
    await s.close()

asyncio.run(main())
