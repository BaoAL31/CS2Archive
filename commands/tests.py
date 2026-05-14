"""
CS2Archive — Pipeline Test CLI Command

Usage:
    python main.py test-pipeline
"""

from __future__ import annotations

import asyncio
import argparse

from rich.console import Console

console = Console(force_terminal=True)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(
        "test-pipeline",
        help="Run full pipeline test: ratings, avatars, demos, CS2DM analysis",
    )


def handle(args: argparse.Namespace) -> None:
    from tests.test_pipeline import run_pipeline

    asyncio.run(run_pipeline())
