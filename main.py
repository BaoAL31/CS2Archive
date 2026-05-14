"""
CS2Archive — CLI Entry Point

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
    python main.py ratings <url> [--top N]
    python main.py test-pipeline
    python main.py trending [--url-only]
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console

from commands import hltv, faceit, player, ratings, status, tests, trending

console = Console(force_terminal=True)

BANNER = """
[bold cyan]+-------------------------------------------+
|           CS2Archive Demo Pipeline           |
|        HLTV & FACEIT Demo Downloader       |
+-------------------------------------------+[/bold cyan]
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cs2recap",
        description="CS2Archive — Download pro CS2 GOTV demos from HLTV and FACEIT",
    )
    subparsers = parser.add_subparsers(dest="source", help="Demo source")

    hltv.register_subparser(subparsers)
    faceit.register_subparser(subparsers)
    player.register_subparser(subparsers)
    ratings.register_subparser(subparsers)
    status.register_subparser(subparsers)
    tests.register_subparser(subparsers)
    trending.register_subparser(subparsers)

    return parser


def main() -> None:
    console.print(BANNER)

    parser = build_parser()
    args = parser.parse_args()

    if not args.source:
        parser.print_help()
        sys.exit(0)

    routing = {
        "hltv": hltv,
        "faceit": faceit,
        "player": player,
        "ratings": ratings,
        "status": status,
        "test-pipeline": tests,
        "trending": trending,
    }

    cmd = routing.get(args.source)
    if cmd:
        cmd.handle(args)


if __name__ == "__main__":
    main()
