"""Allstar-anchored nomination: each demo's top Allstar clip becomes its short.

Thin wrappers over build_foundation.nominate_demo_short, so every nomination
is kill-count verified (exact/partial) and foundation-staged. One nomination
per demo (the demo map's top clip by views), replacing the old heuristic
detector for HLTV extraction.
"""
from __future__ import annotations

from pathlib import Path


def extract_top_allstar_short(demo: Path, timeline: dict | None = None) -> dict | None:
    """Top verified Allstar short for one demo, or None when unmatchable."""
    from cs2archive.config import settings
    from cs2archive.shorts.build_foundation import nominate_demo_short

    nominated = nominate_demo_short(Path(demo), Path(settings.shorts_foundation_dir))
    if not nominated:
        return None
    _timeline, short = nominated
    return short


def extract_match_top_allstar_short(match_id: str, demos: list[Path]):
    """Top verified Allstar short across a match's demos, or None.

    Returns (demo, short) for the highest-viewed nomination.
    """
    best: tuple[Path, dict] | None = None
    best_views = -1.0
    for demo in demos:
        short = extract_top_allstar_short(Path(demo))
        if not short:
            continue
        try:
            views = float((short.get("allstar") or {}).get("views") or 0)
        except (TypeError, ValueError):
            continue
        if views > best_views:
            best, best_views = (Path(demo), short), views
    return best
