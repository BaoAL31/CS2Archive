"""
CS2Archive — HLTV CLI Commands

Usage:
    python main.py hltv match <url>
    python main.py hltv player <name> [--count 5]
    python main.py hltv event <url>
"""

from __future__ import annotations

import asyncio
import argparse
from pathlib import Path

from rich.console import Console

from commands.utils import console, print_match_table, print_result_summary
from player_accounts import get_account


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("hltv", help="Download from HLTV.org")
    sub = parser.add_subparsers(dest="action", help="HLTV action")

    hltv_match = sub.add_parser("match", help="Download demo from a match URL")
    hltv_match.add_argument("url", help="HLTV match page URL")
    hltv_match.add_argument("--force", action="store_true", help="Re-download even if archive exists")
    hltv_match.add_argument("--headless", action="store_true", help="Run CloakBrowser headless (default: visible)")
    hltv_match.add_argument(
        "--profile-dir",
        default=None,
        help="CloakBrowser profile directory (default: .sessions/hltv-cloak)",
    )

    hltv_player = sub.add_parser("player", help="Find & download demos for a player")
    hltv_player.add_argument("name", help="Player name")
    hltv_player.add_argument("--count", type=int, default=5, help="Number of matches (default: 5)")

    hltv_event = sub.add_parser("event", help="Download demos from a tournament")
    hltv_event.add_argument("url", help="HLTV event page URL")


def handle(args: argparse.Namespace) -> None:
    if args.action == "match":
        profile = Path(args.profile_dir) if args.profile_dir else None
        asyncio.run(_cmd_match(args.url, force=args.force, headless=args.headless, profile_dir=profile))
    elif args.action == "player":
        asyncio.run(_cmd_player(args.name, args.count))
    elif args.action == "event":
        asyncio.run(_cmd_event(args.url))


async def _cmd_match(
    url: str,
    *,
    force: bool = False,
    headless: bool = False,
    profile_dir: Path | None = None,
) -> None:
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        result = await scraper.get_match_demo(
            url, force=force, headless=headless, profile_dir=profile_dir,
        )
        print_result_summary([result])
    finally:
        await scraper.close()


async def _cmd_player(name: str, count: int) -> None:
    from scrapers.hltv import HLTVScraper

    steam_id = ""
    account = get_account(name)
    if account and account.steam_id:
        steam_id = account.steam_id
        console.print(f"[dim]   Using saved Steam ID: {steam_id}[/dim]")

    scraper = HLTVScraper()
    try:
        matches = await scraper.search_player_matches(name, count, steam_id=steam_id)
        if not matches:
            return

        print_match_table(matches, f"HLTV Matches for '{name}'")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await scraper.get_match_demo(match.url)
            results.append(result)

        print_result_summary(results)
    finally:
        await scraper.close()


async def _cmd_event(url: str) -> None:
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        matches = await scraper.search_event_matches(url)
        if not matches:
            return

        print_match_table(matches, "Event Matches")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await scraper.get_match_demo(match.url)
            results.append(result)

        print_result_summary(results)
    finally:
        await scraper.close()
