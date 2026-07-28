import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from bs4 import BeautifulSoup
from scrapers.hltv_player_resolver import parse_search_player_candidates

html = Path("search_ropz_debug.html").read_text(encoding="utf-8")
cands = parse_search_player_candidates(html)
print(f"Found {len(cands)} candidates:")
for c in cands:
    print(f"  {c['nickname']} -> {c['player_url']} (id={c['player_id']})")