"""
CS2Archive — FACEIT CLI Commands

Usage:
    python main.py faceit match <match_id_or_room_url>
    python main.py faceit player <name> [--count 5]

Player lookup + match history use the free FACEIT Data API v4 key
(FACEIT_API_KEY). Demo download opens the match room in a logged-in Chrome
profile (.faceit_profile/) and clicks "Watch Demo" — no Downloads API token.
Log in once with `python scripts/faceit_login_launcher.py`.
"""

from __future__ import annotations

import argparse

from rich.console import Console

from commands.utils import print_match_table, print_result_summary

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("faceit", help="Download from FACEIT")
    sub = parser.add_subparsers(dest="action", help="FACEIT action")

    faceit_match = sub.add_parser("match", help="Download demo for a match ID or room URL")
    faceit_match.add_argument("match_id", help="FACEIT match ID or full room URL")

    faceit_player = sub.add_parser("player", help="Find & download demos for a player")
    faceit_player.add_argument("name", help="FACEIT nickname")
    faceit_player.add_argument("--count", type=int, default=5, help="Number of matches (default: 5)")

    faceit_popular = sub.add_parser("popular", help="Download recent demos from curated pros (faceit_pros.json)")
    faceit_popular.add_argument("--count", type=int, default=2, help="Matches per pro (default: 2)")
    faceit_popular.add_argument("--pros", default="faceit_pros.json", help="Path to JSON list of pro nicknames")


def handle(args: argparse.Namespace) -> None:
    if args.action == "match":
        _cmd_match(args.match_id)
    elif args.action == "player":
        import asyncio
        asyncio.run(_cmd_player(args.name, args.count))
    elif args.action == "popular":
        import asyncio
        asyncio.run(_cmd_popular(args.count, args.pros))
    else:
        console.print("[yellow]Usage: python main.py faceit match <id|url> | faceit player <name>[/yellow]")


def _cmd_match(match_id: str) -> None:
    from scrapers.faceit import download_demo

    result = download_demo(match_id)
    print_result_summary([result])


async def _cmd_popular(count: int, pros_path: str) -> None:
    """Download recent matches for a curated list of pros (faceit_pros.json)."""
    import asyncio
    import json
    from pathlib import Path

    from scrapers.faceit import FACEITClient, download_demo

    path = Path(pros_path)
    if not path.exists():
        console.print(f"[red]   [ERR] Pro list not found: {pros_path}[/red]")
        return
    pros = json.loads(path.read_text()).get("pros", [])
    if not pros:
        console.print("[yellow]   No pros listed in config.[/yellow]")
        return

    client = FACEITClient()
    try:
        all_results = []
        for nick in pros:
            console.print(f"\n[bold cyan]Pro:[/bold cyan] {nick}")
            pid = await client.get_player_id(nick)
            if not pid:
                continue
            matches = await client.get_player_matches(pid, limit=count)
            for m in matches:
                all_results.append(download_demo(m.match_id))
        print_result_summary(all_results)
    finally:
        await client.close()


async def _cmd_player(name: str, count: int) -> None:
    import asyncio
    from scrapers.faceit import FACEITClient, download_demo

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

        console.print(f"\n[bold]Downloading {len(matches)} demo(s) (browser scrape)...[/bold]")
        results = []
        for match in matches:
            results.append(download_demo(match.match_id))

        print_result_summary(results)
    finally:
        await client.close()
