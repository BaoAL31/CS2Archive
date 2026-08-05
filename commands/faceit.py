"""
CS2Archive — FACEIT CLI Commands

Usage:
    python main.py faceit match <match_id_or_room_url>
    python main.py faceit player <name> [--count 5]

Player lookup + match history use the free FACEIT Data API v4 key
(FACEIT_API_KEY). Demo download uses the FACEIT Downloads API
(FACEIT_DOWNLOADS_TOKEN, Bearer) when configured, falling back to the
browser scrape (authed Chrome, .sessions/faceit/) if the API is unavailable.
Log in once with `python scripts/faceit/faceit_login_launcher.py` for the fallback.

Recognised Pros come from `.data/player_accounts.json` (single identity store).
"""

from __future__ import annotations

import argparse
from typing import Optional

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

    faceit_popular = sub.add_parser(
        "popular",
        help="Download recent demos for Recognised Pros (.data/player_accounts.json)",
    )
    faceit_popular.add_argument("--count", type=int, default=2, help="Matches per pro (default: 2)")

    faceit_recent = sub.add_parser(
        "recent",
        help="List matches for Recognised Pros within a time window (no download)",
    )
    faceit_recent.add_argument("--hours", type=int, default=48, help="Lookback window in hours (default: 48)")
    faceit_recent.add_argument("--count", type=int, default=20, help="Max matches fetched per pro history (default: 20)")


def handle(args: argparse.Namespace) -> None:
    if args.action == "match":
        _cmd_match(args.match_id)
    elif args.action == "player":
        import asyncio
        asyncio.run(_cmd_player(args.name, args.count))
    elif args.action == "popular":
        import asyncio
        asyncio.run(_cmd_popular(args.count))
    elif args.action == "recent":
        import asyncio
        asyncio.run(_cmd_recent(args.hours, args.count))
    else:
        console.print("[yellow]Usage: python main.py faceit match <id|url> | faceit player <name>[/yellow]")


def _cmd_match(match_id: str) -> None:
    from scrapers.faceit import download_demo

    result = download_demo(match_id)
    print_result_summary([result])


def _recognised_pro_nicks() -> list[str]:
    """Canonical nicknames from player_accounts.json."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    from _pathsetup import ensure
    ensure()
    from player_accounts import list_accounts

    return [a.nickname for a in list_accounts() if a.nickname]


async def _cmd_recent(hours: int, count: int) -> None:
    """List recent matches for Recognised Pros within a time window (report only)."""
    from datetime import datetime, timedelta

    from scrapers.faceit import FACEITClient

    pros = _recognised_pro_nicks()
    if not pros:
        console.print("[yellow]   No Recognised Pros in .data/player_accounts.json.[/yellow]")
        return

    client = FACEITClient()
    try:
        from faceit_names import (
            known_pro_faceit_ids, known_pro_steam_ids, faceit_nick,
        )
        known_fids = known_pro_faceit_ids()
        known_sids = known_pro_steam_ids()

        cutoff = datetime.now() - timedelta(hours=hours)
        all_recent: list = []
        match_pros: dict[str, set] = {}
        _steam_cache: dict[str, Optional[str]] = {}

        async def _lobby_steam_id(player_id: str) -> Optional[str]:
            if player_id not in _steam_cache:
                _steam_cache[player_id] = await client.get_player_steam_id(player_id)
            return _steam_cache[player_id]

        for nick in pros:
            query = faceit_nick(nick)
            pid = await client.get_player_id(query)
            if not pid:
                continue
            matches = await client.get_player_matches(pid, limit=count)
            recent = [m for m in matches if m.date and m.date >= cutoff]
            if not recent:
                continue
            for m in recent:
                stats = await client.get_match_stats(m.match_id)
                if not stats:
                    continue
                m.map_name = stats.get("map", m.map_name) or m.map_name
                m.score = stats.get("score", m.score) or m.score
                line = stats.get("players", {}).get(nick) or stats.get("players", {}).get(query)
                if line:
                    m.player_kd = str(line.get("kd", ""))
                    m.player_adr = str(line.get("adr", ""))
                    m.player_hs = str(line.get("hs", ""))
                    m.player_kills = str(line.get("kills", ""))
                    m.player_deaths = str(line.get("deaths", ""))
                match_pros.setdefault(m.match_id, set())
                if pid in known_fids:
                    match_pros[m.match_id].add(known_fids[pid])
                for pl in stats.get("players", {}).values():
                    pl_id = pl.get("player_id")
                    if pl_id and pl_id in known_fids:
                        match_pros[m.match_id].add(known_fids[pl_id])
                        continue
                    sid = await _lobby_steam_id(pl_id) if pl_id else None
                    if sid and sid in known_sids:
                        match_pros[m.match_id].add(known_sids[sid])
                elo_sum = 0
                elo_n = 0
                for pl in stats.get("players", {}).values():
                    pl_id = pl.get("player_id")
                    if pl_id:
                        e = await client.get_player_elo(pl_id)
                        if e is not None:
                            elo_sum += e
                            elo_n += 1
                if elo_n:
                    m.match_elo = elo_sum // elo_n
            console.print(f"\n[bold cyan]Pro:[/bold cyan] {nick}")
            print_match_table(recent, f"{nick} — last {hours}h")
            all_recent.extend(recent)
        multi = {mid: ps for mid, ps in match_pros.items() if len(ps) >= 2}
        if multi:
            console.print(f"\n[bold magenta]★ Matches with multiple known pros:[/bold magenta]")
            for mid, ps in sorted(multi.items(), key=lambda x: -len(x[1])):
                canon = ", ".join(sorted(ps))
                console.print(f"  [magenta]{mid}[/magenta]  {canon}")
        console.print(
            f"\n[bold]Total matches in window:[/bold] {len(all_recent)}  "
            f"(multi-pro: {len(multi)})"
        )
    finally:
        await client.close()


async def _cmd_popular(count: int) -> None:
    """Download recent matches for Recognised Pros (player_accounts.json)."""
    from scrapers.faceit import FACEITClient, download_demo
    from faceit_names import faceit_nick

    pros = _recognised_pro_nicks()
    if not pros:
        console.print("[yellow]   No Recognised Pros in .data/player_accounts.json.[/yellow]")
        return

    client = FACEITClient()
    try:
        all_results = []
        for nick in pros:
            console.print(f"\n[bold cyan]Pro:[/bold cyan] {nick}")
            query = faceit_nick(nick)
            pid = await client.get_player_id(query)
            if not pid:
                continue
            matches = await client.get_player_matches(pid, limit=count)
            for m in matches:
                all_results.append(download_demo(m.match_id))
        print_result_summary(all_results)
    finally:
        await client.close()


async def _cmd_player(name: str, count: int) -> None:
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
