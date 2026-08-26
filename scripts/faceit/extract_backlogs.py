"""Unified FACEIT extraction entry point.

Replaces direct use of ``create_faceit_backlog.py``. For a FACEIT demo + POV
player it performs BOTH:

  1. FACEIT backlog extraction  -> single-POV backlog card
     (delegates to ``create_faceit_backlog.create_faceit_backlog``)
  2. Shorts extraction         -> 4K / clutch short timelines for that POV
     (delegates to ``scripts.shorts.build_short_timeline``)

Usage:
    python scripts/faceit/extract_backlogs.py <demo_path> --player <nick> --map <map>
        [--steam-id <id>] [--priority high|mid|low] [--match-id <id>]
        [--tournament <name>] [--match-date YYYY-MM-DD]
        [--no-elo] [--no-shorts] [--include-all-players]

``--player`` is the POV player (single POV only — not a whole-match card).
Pass ``--no-shorts`` to skip short extraction.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from scripts.faceit.create_faceit_backlog import create_faceit_backlog  # noqa: E402


def _extract_shorts(demo: Path, steam_id: str, include_all_players: bool) -> None:
    from scripts.shorts.build_short_timeline import (
        build_short_timeline,
        _build_short_slug,
    )
    from scripts.shorts import resolve_output_dir

    pros_only = not include_all_players
    timeline = build_short_timeline(demo, player=steam_id, pros_only=pros_only)
    timeline = {k: v for k, v in timeline.items() if k != "_dropped_randos"}
    shorts = timeline.get("shorts", [])
    if not shorts:
        dropped = timeline.get("_dropped_randos", 0)
        print(f"[OK] 0 shorts detected"
              + (f" ({dropped} non-pro short(s) filtered)" if dropped else "")
              + " (no output written)")
        return
    base = resolve_output_dir(demo, player=steam_id)
    written = 0
    for short in shorts:
        slug = _build_short_slug(short)
        short_dir = base / f"shorts-{slug}"
        short_dir.mkdir(parents=True, exist_ok=True)
        single = {**timeline, "short_count": 1, "shorts": [short]}
        (short_dir / "short_timeline.json").write_text(
            json.dumps(single, indent=2), encoding="utf-8")
        written += 1
    print(f"[OK] {len(shorts)} shorts -> {written} file(s) under {base}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("demo_path")
    ap.add_argument("--player", required=True,
                    help="POV player nickname (single POV, not whole match)")
    ap.add_argument("--map", default="", help="Map display name (auto-detected if omitted)")
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--tournament", default="")
    ap.add_argument("--match-id", default="",
                    help="FACEIT match id (auto-resolved from download history if omitted)")
    ap.add_argument("--priority", choices=["high", "mid", "low"], default="high")
    ap.add_argument("--match-date", help="Match date YYYY-MM-DD (defaults to demo file date)")
    ap.add_argument("--no-elo", action="store_true",
                    help="Skip FACEIT ELO fetch (title/thumbnail omit ELO line)")
    ap.add_argument("--no-shorts", action="store_true",
                    help="Skip Shorts (4K/clutch) timeline extraction")
    ap.add_argument("--include-all-players", action="store_true",
                    help="Keep shorts for any player (default: Recognised Pros only)")
    args = ap.parse_args()

    backlog_file = create_faceit_backlog(
        demo_path=args.demo_path,
        player=args.player,
        map=args.map,
        steam_id=args.steam_id,
        tournament=args.tournament,
        match_id=args.match_id,
        priority=args.priority,
        match_date=args.match_date,
        no_elo=args.no_elo,
    )

    if args.no_shorts:
        print("[SKIP] shorts extraction disabled (--no-shorts)")
        return

    meta = json.loads(Path(backlog_file).read_text(encoding="utf-8"))
    steam_id = meta.get("steam_id") or args.steam_id
    if not steam_id:
        print("[WARN] no steam_id available; skipping shorts extraction")
        return
    print("[SHORTS] Extracting 4K/clutch timelines ...")
    _extract_shorts(Path(args.demo_path).resolve(), steam_id, args.include_all_players)


if __name__ == "__main__":
    main()
