"""
CS2Archive — Player Account CLI Commands

Handles `player add`, `player list`, `player show`, `player remove`.
"""

from __future__ import annotations

import argparse

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from player_accounts import add_account, get_account, list_accounts, remove_account

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("player", help="Manage saved player accounts")
    sub = parser.add_subparsers(dest="action", help="Player action")

    add_p = sub.add_parser("add", help="Add or update a player account")
    add_p.add_argument("nickname", help="Display name / alias")
    add_p.add_argument("--faceit", dest="faceit_url", default="", help="Faceit profile URL")
    add_p.add_argument("--steam", dest="steam_url", default="", help="Steam profile URL")

    show_p = sub.add_parser("show", help="Show a player account")
    show_p.add_argument("nickname", help="Player nickname")

    rm_p = sub.add_parser("remove", help="Remove a player account")
    rm_p.add_argument("nickname", help="Player nickname")

    sub.add_parser("list", help="List all saved player accounts")


def handle(args: argparse.Namespace) -> None:
    if args.action == "add":
        _add(args.nickname, args.faceit_url, args.steam_url)
    elif args.action == "list":
        _list()
    elif args.action == "show":
        _show(args.nickname)
    elif args.action == "remove":
        _remove(args.nickname)


def _add(nickname: str, faceit_url: str, steam_url: str) -> None:
    account = add_account(nickname, faceit_url, steam_url)
    console.print(f"\n[bold green][OK] Player '{nickname}' saved[/bold green]")
    _print_account(account)


def _list() -> None:
    accounts = list_accounts()
    if not accounts:
        console.print("\n[yellow]No player accounts saved yet.[/yellow]")
        return

    table = Table(title="Saved Player Accounts", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Nickname", style="cyan", width=16)
    table.add_column("Faceit", style="white", min_width=20)
    table.add_column("Steam", style="white", min_width=20)

    for i, acc in enumerate(accounts, 1):
        faceit = acc.faceit_nickname or acc.faceit_url or "[dim]—[/dim]"
        steam = acc.steam_url or "[dim]—[/dim]"
        table.add_row(str(i), acc.nickname, faceit, steam)

    console.print()
    console.print(table)


def _show(nickname: str) -> None:
    account = get_account(nickname)
    if not account:
        console.print(f"\n[red][ERR] Player '{nickname}' not found[/red]")
        return
    _print_account(account)


def _remove(nickname: str) -> None:
    if remove_account(nickname):
        console.print(f"\n[bold green][OK] Player '{nickname}' removed[/bold green]")
    else:
        console.print(f"\n[red][ERR] Player '{nickname}' not found[/red]")


def _print_account(account) -> None:
    lines = [
        f"[bold]Nickname:[/bold] {account.nickname}",
    ]
    if account.faceit_url:
        faceit_display = account.faceit_nickname or account.faceit_url
        lines.append(f"[bold]Faceit:[/bold] {faceit_display}")
        lines.append(f"[dim]  {account.faceit_url}[/dim]")
    if account.steam_url:
        lines.append(f"[bold]Steam:[/bold] {account.steam_url}")
    lines.append(f"[dim]Saved: {account.created_at.strftime('%Y-%m-%d %H:%M')}[/dim]")

    console.print()
    console.print(Panel("\n".join(lines), title="Player Account", border_style="cyan"))
