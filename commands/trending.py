"""
CS2Archive — Trending Match CLI Command

Usage:
    python main.py trending [--url-only] [--count N]
"""

from __future__ import annotations

import asyncio
import argparse

from rich.console import Console
from rich.table import Table

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "trending",
        help="Find trending CS2 matches from highlight channels",
    )
    parser.add_argument("--url-only", action="store_true", help="Print top HLTV URL only")
    parser.add_argument("--count", type=int, default=3, help="Number to show (default: 3)")


def handle(args: argparse.Namespace) -> None:
    asyncio.run(_cmd(args.url_only, args.count))


async def _cmd(url_only: bool, count: int) -> None:
    from scrapers.trending import find_trending

    results = await find_trending(count=count)
    if not results:
        return

    if url_only:
        console.print(results[0]["hltv_url"])
        return

    table = Table(title="Trending CS2 Matches (last 24h)", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Channel", style="cyan")
    table.add_column("Views", style="green", justify="right")
    table.add_column("Match", style="white")

    for i, r in enumerate(results, 1):
        t1, t2 = r["teams"]
        table.add_row(str(i), r["channel"], f"{r['views']:,}", f"{t1} vs {t2}")

    console.print()
    console.print(table)
    console.print()
    for i, r in enumerate(results, 1):
        console.print(f"  {i}. {r['hltv_url']}")
