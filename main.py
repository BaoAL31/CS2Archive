"""
CS2Archive — CLI Entry Point

Rich CLI interface for downloading CS2 GOTV demos from HLTV and FACEIT.

Usage:
    python main.py hltv match <url>
    python main.py hltv player <name> [--count 5]
    python main.py hltv event <url>
    python main.py faceit match <match_id>
    python main.py faceit player <name> [--count 5]
    python main.py player add <nickname> [--faceit URL] [--steam URL]
    python main.py player list
    python main.py player show <nickname>
    python main.py player remove <nickname>
    python main.py status
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Force UTF-8 output on Windows to avoid charmap encoding errors
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from config import settings
from downloader import get_download_history
from models import DownloadStatus
from commands import player as player_cmd

console = Console(force_terminal=True)

BANNER = """
[bold cyan]+-------------------------------------------+
|           CS2Archive Demo Pipeline           |
|        HLTV & FACEIT Demo Downloader       |
+-------------------------------------------+[/bold cyan]
"""


# ── HLTV Commands ─────────────────────────────────────────────────────────────


async def cmd_hltv_match(url: str) -> None:
    """Download a demo from a specific HLTV match URL."""
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        result = await scraper.get_match_demo(url)
        _print_result_summary([result])
    finally:
        await scraper.close()


async def cmd_hltv_player(name: str, count: int) -> None:
    """Find and download recent demos for a player from HLTV."""
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        matches = await scraper.search_player_matches(name, count)
        if not matches:
            return

        _print_match_table(matches, f"HLTV Matches for '{name}'")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await scraper.get_match_demo(match.url)
            results.append(result)

        _print_result_summary(results)
    finally:
        await scraper.close()


async def cmd_hltv_event(url: str) -> None:
    """Download all demos from an HLTV event/tournament."""
    from scrapers.hltv import HLTVScraper

    scraper = HLTVScraper()
    try:
        matches = await scraper.search_event_matches(url)
        if not matches:
            return

        _print_match_table(matches, "Event Matches")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await scraper.get_match_demo(match.url)
            results.append(result)

        _print_result_summary(results)
    finally:
        await scraper.close()


# ── FACEIT Commands ───────────────────────────────────────────────────────────


async def cmd_faceit_match(match_id: str) -> None:
    """Download a demo for a specific FACEIT match."""
    from scrapers.faceit import FACEITClient

    client = FACEITClient()
    try:
        result = await client.download_demo(match_id)
        _print_result_summary([result])
    finally:
        await client.close()


async def cmd_faceit_player(name: str, count: int) -> None:
    """Find and download recent demos for a player from FACEIT."""
    from scrapers.faceit import FACEITClient

    client = FACEITClient()
    try:
        console.print(f"\n[bold cyan]🔍 Looking up FACEIT player:[/bold cyan] {name}")
        player_id = await client.get_player_id(name)
        if not player_id:
            return

        matches = await client.get_player_matches(player_id, limit=count)
        if not matches:
            console.print("[yellow]   No recent matches found.[/yellow]")
            return

        _print_match_table(matches, f"FACEIT Matches for '{name}'")

        console.print(f"\n[bold]Downloading {len(matches)} demo(s)...[/bold]")
        results = []
        for match in matches:
            result = await client.download_demo(match.match_id)
            results.append(result)

        _print_result_summary(results)
    finally:
        await client.close()


# ── Status Command ────────────────────────────────────────────────────────────


def cmd_status() -> None:
    """Show download history and statistics."""
    records = get_download_history()

    if not records:
        console.print("\n[yellow]No downloads yet. Use 'hltv' or 'faceit' commands to get started.[/yellow]")
        return

    table = Table(title="Download History", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Source", style="cyan", width=8)
    table.add_column("Match", style="white", min_width=30)
    table.add_column("Size", style="green", width=10, justify="right")
    table.add_column("Date", style="dim", width=12)

    total_size = 0.0
    for i, record in enumerate(records, 1):
        total_size += record.file_size_mb
        table.add_row(
            str(i),
            record.source.value.upper(),
            record.match_display,
            f"{record.file_size_mb:.1f} MB",
            record.downloaded_at.strftime("%Y-%m-%d"),
        )

    console.print()
    console.print(table)

    # Summary stats
    hltv_count = sum(1 for r in records if r.source.value == "hltv")
    faceit_count = sum(1 for r in records if r.source.value == "faceit")
    console.print(
        f"\n[bold]Total:[/bold] {len(records)} demos "
        f"({hltv_count} HLTV, {faceit_count} FACEIT) — "
        f"{total_size:.1f} MB"
    )
    console.print(f"[dim]Storage: {settings.demo_storage_dir.resolve()}[/dim]")




# ── Display Helpers ───────────────────────────────────────────────────────────


def _print_match_table(matches: list, title: str) -> None:
    """Print a table of matches."""
    table = Table(title=title, show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Match ID", style="cyan", width=12)
    table.add_column("Teams", style="white", min_width=25)
    table.add_column("Map", style="magenta", width=12)
    table.add_column("Date", style="dim", width=12)

    for i, match in enumerate(matches, 1):
        date_str = match.date.strftime("%Y-%m-%d") if match.date else "-"
        teams = f"{match.team1} vs {match.team2}"
        table.add_row(str(i), match.match_id[:12], teams, match.map_name, date_str)

    console.print()
    console.print(table)


def _print_result_summary(results: list) -> None:
    """Print a summary of download results."""
    success = [r for r in results if r.status == DownloadStatus.COMPLETED]
    skipped = [r for r in results if r.status == DownloadStatus.SKIPPED]
    failed = [r for r in results if r.status == DownloadStatus.FAILED]

    console.print()
    parts = []
    if success:
        parts.append(f"[green][OK] {len(success)} downloaded[/green]")
    if skipped:
        parts.append(f"[yellow][SKIP] {len(skipped)} skipped[/yellow]")
    if failed:
        parts.append(f"[red][FAIL] {len(failed)} failed[/red]")

    console.print(Panel(
        " · ".join(parts) if parts else "[dim]No results[/dim]",
        title="Results",
        border_style="cyan",
    ))

    for r in failed:
        console.print(f"  [red]• {r.match.display_name}: {r.error}[/red]")


# ── Argument Parsing ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cs2recap",
        description="CS2Archive — Download pro CS2 GOTV demos from HLTV and FACEIT",
    )
    subparsers = parser.add_subparsers(dest="source", help="Demo source")

    # HLTV
    hltv_parser = subparsers.add_parser("hltv", help="Download from HLTV.org")
    hltv_sub = hltv_parser.add_subparsers(dest="action", help="HLTV action")

    hltv_match = hltv_sub.add_parser("match", help="Download demo from a match URL")
    hltv_match.add_argument("url", help="HLTV match page URL")

    hltv_player = hltv_sub.add_parser("player", help="Find & download demos for a player")
    hltv_player.add_argument("name", help="Player name")
    hltv_player.add_argument("--count", type=int, default=5, help="Number of matches (default: 5)")

    hltv_event = hltv_sub.add_parser("event", help="Download demos from a tournament")
    hltv_event.add_argument("url", help="HLTV event page URL")

    # FACEIT
    faceit_parser = subparsers.add_parser("faceit", help="Download from FACEIT")
    faceit_sub = faceit_parser.add_subparsers(dest="action", help="FACEIT action")

    faceit_match = faceit_sub.add_parser("match", help="Download demo for a match ID")
    faceit_match.add_argument("match_id", help="FACEIT match ID")

    faceit_player = faceit_sub.add_parser("player", help="Find & download demos for a player")
    faceit_player.add_argument("name", help="FACEIT nickname")
    faceit_player.add_argument("--count", type=int, default=5, help="Number of matches (default: 5)")

    # Player Accounts
    player_cmd.register_subparser(subparsers)

    # Status
    subparsers.add_parser("status", help="Show download history & stats")

    return parser


def main() -> None:
    console.print(BANNER)

    parser = build_parser()
    args = parser.parse_args()

    if not args.source:
        parser.print_help()
        sys.exit(0)

    if args.source == "status":
        cmd_status()
        return

    if not hasattr(args, "action") or not args.action:
        parser.parse_args([args.source, "--help"])
        sys.exit(0)

    # Route to the correct async command
    if args.source == "hltv":
        if args.action == "match":
            asyncio.run(cmd_hltv_match(args.url))
        elif args.action == "player":
            asyncio.run(cmd_hltv_player(args.name, args.count))
        elif args.action == "event":
            asyncio.run(cmd_hltv_event(args.url))

    elif args.source == "faceit":
        if args.action == "match":
            asyncio.run(cmd_faceit_match(args.match_id))
        elif args.action == "player":
            asyncio.run(cmd_faceit_player(args.name, args.count))

    elif args.source == "player":
        player_cmd.handle(args)


if __name__ == "__main__":
    main()
