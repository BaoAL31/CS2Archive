"""
CS2Archive — FACEIT CLI Commands

Usage:
    python main.py faceit match <match_id>
    python main.py faceit player <name> [--count 5]
"""

from __future__ import annotations

import asyncio
import argparse

from rich.console import Console

from commands.utils import print_match_table, print_result_summary

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("faceit", help="Download from FACEIT")
    sub = parser.add_subparsers(dest="action", help="FACEIT action")

    faceit_match = sub.add_parser("match", help="Download demo for a match ID")
    faceit_match.add_argument("match_id", help="FACEIT match ID")

    faceit_player = sub.add_parser("player", help="Find & download demos for a player")
    faceit_player.add_argument("name", help="FACEIT nickname")
    faceit_player.add_argument("--count", type=int, default=5, help="Number of matches (default: 5)")


def handle(args: argparse.Namespace) -> None:
    if args.action == "match":
        asyncio.run(_cmd_match(args.match_id))
    elif args.action == "player":
        asyncio.run(_cmd_player(args.name, args.count))


async def _cmd_match(match_id: str) -> None:
    from scrapers.faceit import FACEITClient

    client = FACEITClient()
    try:
        result = await client.download_demo(match_id)
        print_result_summary([result])
    finally:
        await client.close()


async def _cmd_player(name: str, count: int) -> None:
    from scrapers.faceit import FACEITClient

    client = FACEITClient()
    try:
        console.print(f"\n[bold cyan]Looking up FACEIT player:[/bold cyan] {name}")
        player_id = await client.get_player_id(name)
        if not player_id:
            return

        matches = await client.get_player_matches(player_id, limit=count)
        if not matches:
            console.print("[yellow]   No recent matches found.[/yellow]")
            return

        print_match_table(matches, f"FACEIT Matches for '{name}'")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await client.download_demo(match.match_id)
            results.append(result)

        print_result_summary(results)
    finally:
        await client.close()
