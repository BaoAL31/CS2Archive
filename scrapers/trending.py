"""
CS2Archive — Trending Match Finder

1. Gets top highlight videos from all CS2 highlight channels (configurable window)
2. Matches them to HLTV matches by scanning HLTV results page
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from rich.console import Console
from bs4 import BeautifulSoup

from config import settings

console = Console(force_terminal=True)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEO_URL = "https://www.googleapis.com/youtube/v3/videos"

HIGHLIGHT_CHANNELS = {
    "UC3y-TwdfrUfm7Iindvvxsjg": "PGL CS2 Highlights",
    "UCDQZcZZwv-RhxHxJpHCdmbQ": "ESL CS2 Highlights",
    "UCbWA4nLSXvfWnOS2Q4yP48A": "BLAST CS2 Highlights",
    "UCBXeecyxQv7GblaybCxQgRA": "StarLadder CS2 Highlights",
}

TEAM_ALIASES = {
    "navi": "natus vincere",
    "bb": "betboom",
    "bbteam": "betboom",
    "bb team": "betboom",
    "b b team": "betboom",
    "pvision": "parivision",
    "themongolz": "the mongolz",
    "nrg": "nrg",
    "bcgame": "bc game",
    "gamerlegion": "gamerlegion",
}

EVENT_KEYWORDS = [
    "pgl", "blast", "esl", "iem", "cct", "star", "major", "rmr",
    "astana", "atlanta", "rotterdam", "krakow", "rio", "cluj",
    "napoca", "budapest", "open", "season", "finals", "playoffs",
    "group", "stage", "qualifier", "championship", "series",
    "odyssey", "cup", "league", "masters", "hero", "asian",
    "europe", "south", "america", "downunder", "clutch",
    "2024", "2025", "2026",
]


def _extract_teams(title: str) -> Optional[tuple[str, str]]:
    part = title.split(" - ")[0].split(" |")[0]
    part = re.split(r"[!|]\s*", part)[-1]
    m = re.search(
        r"([A-Za-z0-9_.\s]+?)\s+(?:vs\.?|v\.?)\s+([A-Za-z0-9_.\s]+?)$",
        part,
        re.IGNORECASE,
    )
    if m:
        t1 = m.group(1).strip().rstrip(".,:;!")
        t2 = m.group(2).strip().rstrip(".,:;!")
        if 2 <= len(t1) <= 25 and 2 <= len(t2) <= 25:
            return (t1, t2)
    return None


def _normalize(name: str) -> str:
    return name.lower().replace("-", "").replace(" ", "").replace(".", "").replace("_", "").replace("'", "")


def _strip_event(slug_part: str) -> str:
    words = slug_part.split("-")
    result = []
    for w in words:
        if w.lower() in EVENT_KEYWORDS:
            break
        result.append(w)
    return "-".join(result)


def _apply_alias(name: str) -> str:
    n = _normalize(name)
    if n in TEAM_ALIASES:
        return TEAM_ALIASES[n]
    words = n.split()
    if len(words) > 1:
        combined = "".join(words)
        if combined in TEAM_ALIASES:
            return TEAM_ALIASES[combined]
    return n


def _teams_match(yt_t1: str, yt_t2: str, slug: str) -> bool:
    slug_norm = _normalize(slug)
    y1_norm = _normalize(_apply_alias(yt_t1))
    y2_norm = _normalize(_apply_alias(yt_t2))

    if y1_norm in slug_norm and y2_norm in slug_norm:
        return True

    slug_parts = slug.split("-vs-")
    if len(slug_parts) != 2:
        return False
    s1_clean = _normalize(_strip_event(slug_parts[0]))
    s2_clean = _normalize(_strip_event(slug_parts[1]))
    return (y1_norm == s1_clean and y2_norm == s2_clean) or (y1_norm == s2_clean and y2_norm == s1_clean)


def _parse_hltv_match_links(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    matches = []
    seen = set()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        m = re.match(r".*/matches/(\d+)/(.+)", href)
        if not m or href in seen:
            continue
        seen.add(href)
        matches.append({
            "match_id": m.group(1),
            "slug": m.group(2),
            "url": f"{settings.hltv_base_url}/matches/{m.group(1)}/{m.group(2)}",
        })
    return matches


async def get_hltv_matches() -> list[dict]:
    from scrapers.hltv_acquire import fetch_hltv_page_html

    url = f"{settings.hltv_base_url}/results"
    console.print("[cyan]Loading HLTV results via CloakBrowser...[/cyan]")
    html = await asyncio.to_thread(fetch_hltv_page_html, url)
    return _parse_hltv_match_links(html)


def _pick_hltv_match(yt_t1: str, yt_t2: str, candidates: list[dict]) -> dict | None:
    for h in candidates:
        if _teams_match(yt_t1, yt_t2, h["slug"]):
            return h
    return None


async def _search_hltv_match(yt_t1: str, yt_t2: str) -> dict | None:
    from scrapers.hltv_acquire import fetch_hltv_page_html

    query = quote(f"{yt_t1} vs {yt_t2}")
    url = f"{settings.hltv_base_url}/search?query={query}"
    html = await asyncio.to_thread(fetch_hltv_page_html, url)
    candidates = _parse_hltv_match_links(html)
    return _pick_hltv_match(yt_t1, yt_t2, candidates)


async def get_top_videos(hours: int = 24) -> list[dict]:
    if not settings.youtube_api_key:
        console.print("[red]YOUTUBE_API_KEY not configured[/red]")
        return []

    console.print(f"[bold cyan]Fetching top CS2 highlight videos (last {hours}h)...[/bold cyan]")

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    max_results = 5 if hours <= 24 else 10
    all_videos = []

    async with httpx.AsyncClient(timeout=15) as client:
        for cid, cname in HIGHLIGHT_CHANNELS.items():
            r = await client.get(YOUTUBE_SEARCH_URL, params={
                "part": "snippet", "channelId": cid, "type": "video",
                "order": "viewCount", "publishedAfter": since, "maxResults": max_results,
                "key": settings.youtube_api_key,
            })
            r.raise_for_status()
            items = r.json().get("items", [])
            video_ids = [i["id"]["videoId"] for i in items if "videoId" in i.get("id", {})]
            if not video_ids:
                continue
            s = await client.get(YOUTUBE_VIDEO_URL, params={
                "part": "snippet,statistics",
                "id": ",".join(video_ids),
                "key": settings.youtube_api_key,
            })
            s.raise_for_status()
            for item in s.json().get("items", []):
                title = item["snippet"]["title"]
                teams = _extract_teams(title)
                if not teams:
                    continue
                try:
                    views = int(item["statistics"]["viewCount"])
                except (ValueError, TypeError):
                    views = 0
                all_videos.append({
                    "channel": cname,
                    "title": title,
                    "teams": teams,
                    "views": views,
                    "url": f"https://youtube.com/watch?v={item['id']}",
                })

    all_videos.sort(key=lambda x: -x["views"])
    return all_videos


async def find_trending(count: int = 3, hours: int = 24) -> Optional[list[dict]]:
    videos = await get_top_videos(hours=hours)
    if not videos:
        console.print("[red]No highlight videos found[/red]")
        return None

    console.print(f"[green]Found {len(videos)} highlight video(s)[/green]")

    hltv_matches = await get_hltv_matches()
    if not hltv_matches:
        console.print("[red]No HLTV matches found[/red]")
        return None

    console.print(f"[green]Loaded {len(hltv_matches)} HLTV match(es)[/green]")

    results = []
    for v in videos:
        if len(results) >= count:
            break
        yt_t1, yt_t2 = v["teams"]
        best = _pick_hltv_match(yt_t1, yt_t2, hltv_matches)
        if not best:
            console.print(f"[yellow]Searching HLTV for {yt_t1} vs {yt_t2}...[/yellow]")
            best = await _search_hltv_match(yt_t1, yt_t2)
        if best:
            results.append({**v, "hltv_url": best["url"], "hltv_slug": best["slug"]})

    if not results:
        console.print("[yellow]Could not match any videos to HLTV[/yellow]")
        return None

    console.print(f"[green]Matched {len(results)} video(s) to HLTV[/green]")
    return results
