"""
CS2Archive — Status CLI Command

Usage:
    python main.py status
"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from config import settings
from downloader import get_download_history

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser("status", help="Show download history & stats")


def handle(args: argparse.Namespace) -> None:
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

    hltv_count = sum(1 for r in records if r.source.value == "hltv")
    faceit_count = sum(1 for r in records if r.source.value == "faceit")
    console.print(
        f"\n[bold]Total:[/bold] {len(records)} demos "
        f"({hltv_count} HLTV, {faceit_count} FACEIT) — "
        f"{total_size:.1f} MB"
    )
    console.print(f"[dim]Storage: {settings.demo_storage_dir.resolve()}[/dim]")
