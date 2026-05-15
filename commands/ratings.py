"""
CS2Archive — HLTV Ratings CLI Command

Usage:
    python main.py ratings <match_url>
"""

from __future__ import annotations

import asyncio
import argparse
import json
import re
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console(force_terminal=True)

ANALYSIS_DIR = Path("demos/analysis")


def _slug_from_url(url: str) -> str | None:
    m = re.search(r"/matches/\d+/([^/?#]+)", url)
    if m:
        return m.group(1)
    return None


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "ratings",
        help="Show HLTV Rating 3.0 stats for a match",
    )
    parser.add_argument("url", help="HLTV match page URL")
    parser.add_argument("--top", type=int, default=0, help="Show only top N players by rating")


def handle(args: argparse.Namespace) -> None:
    asyncio.run(_cmd(args.url, args.top))


async def _cmd(url: str, top: int) -> None:
    from scrapers.ratings import get_match_ratings

    result = await get_match_ratings(url)
    if not result:
        console.print("[red]No ratings found[/red]")
        return

    slug = _slug_from_url(url)
    if slug:
        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        ratings_path = ANALYSIS_DIR / f"{slug}_ratings.json"
        ratings_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        console.print(f"[dim]  Saved to {ratings_path}[/dim]")

    seen = set()
    for table in result["tables"]:
        unique_players = []
        for p in table["players"]:
            key = (p["nickname"], p["rating"])
            if key not in seen:
                seen.add(key)
                unique_players.append(p)

        if not unique_players:
            continue

        if top > 0:
            unique_players = sorted(unique_players, key=lambda x: float(x["rating"]), reverse=True)[:top]

        unique_players.sort(key=lambda x: float(x["rating"]), reverse=True)

        t = Table(title=f"{table['team']} — {table['map']}", show_lines=True)
        t.add_column("Player", style="cyan", width=16)
        t.add_column("K-D", width=8)
        t.add_column("Swing", width=9)
        t.add_column("ADR", width=6)
        t.add_column("KAST", width=6)
        t.add_column("Rating 3.0", style="green", width=10, justify="right")

        for p in unique_players:
            t.add_row(p["nickname"], p["kd"], p["swing"], p["adr"], p["kast"], p["rating"])

        console.print()
        console.print(t)
