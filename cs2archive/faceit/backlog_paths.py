"""Canonical date-partitioned FACEIT backlog paths."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

def match_date_for_demo(demo: Path, explicit: str | None = None) -> str:
    if explicit:
        datetime.strptime(explicit, '%Y-%m-%d')
        return explicit
    return datetime.fromtimestamp(demo.stat().st_mtime).strftime('%Y-%m-%d')

def faceit_backlog_dir(root: Path, date: str, priority: str) -> Path:
    return root / 'faceit' / date / priority
