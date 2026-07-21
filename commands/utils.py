"""
CS2Archive — Shared CLI display helpers
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from models import DownloadResult, DownloadStatus, MatchInfo

console = Console(force_terminal=True)


def print_match_table(matches: list[MatchInfo], title: str) -> None:
    table = Table(title=title, show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Match ID", style="cyan", width=12)
    table.add_column("Teams", style="white", min_width=25)
    table.add_column("Map", style="magenta", width=12)
    table.add_column("Date", style="dim", width=12)
    show_stats = any(m.player_kd for m in matches)
    if show_stats:
        table.add_column("K/D", style="green", width=8)
        table.add_column("ADR", style="yellow", width=7)
        table.add_column("HS%", style="blue", width=6)
        table.add_column("ELO", style="magenta", width=7)

    for i, match in enumerate(matches, 1):
        date_str = match.date.strftime("%Y-%m-%d") if match.date else "-"
        teams = f"{match.team1} vs {match.team2}"
        row = [str(i), match.match_id[:12], teams, match.map_name, date_str]
        if show_stats:
            row += [match.player_kd or "-", match.player_adr or "-", match.player_hs or "-",
                    str(match.match_elo) if match.match_elo else "-"]
        table.add_row(*row)

    console.print()
    console.print(table)


def print_result_summary(results: list[DownloadResult]) -> None:
    from rich.panel import Panel

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
