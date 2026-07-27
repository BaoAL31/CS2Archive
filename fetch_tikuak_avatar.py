"""Fetch avatar for tikuak."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from scrapers.player_images import CloakAvatarFetcher, _fetch_avatar_cloak

url = "https://www.hltv.org/matches/2396008/wildcard-vs-the-mongolz-blast-bounty-2026-season-2"
ratings_path = "demos/analysis/wildcard-vs-the-mongolz-blast-bounty-2026-season-2_ratings.json"

with CloakAvatarFetcher(headless=False) as fetcher:
    path = _fetch_avatar_cloak("tikuak", url, ratings_path, fetcher=fetcher)
    print(f"Avatar saved: {path}")
