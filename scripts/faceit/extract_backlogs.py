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
        persist_action_timeline,
        short_json_payload,
    )
    from scripts.shorts import discard_empty_shorts_dir, resolve_output_dir

    pros_only = not include_all_players
    timeline = build_short_timeline(demo, player=steam_id, pros_only=pros_only)
    dropped_randos = timeline.get("_dropped_randos", 0)
    dropped_demand = 0
    shorts = timeline.get("shorts", [])
    if pros_only:
        from shorts.demand_gate import filter_publishable_shorts, folder_orgs
        shorts, dropped_demand = filter_publishable_shorts(
            shorts, orgs=folder_orgs(demo), source="faceit",
        )
    from shorts.demand_gate import filter_suffix
    base = resolve_output_dir(demo, player=steam_id)
    suffix = filter_suffix(dropped_randos, dropped_demand, source="faceit")
    if not shorts:
        discard_empty_shorts_dir(base)
        print(f"[OK] 0 shorts detected{suffix}")
        return
    persist_action_timeline(demo, timeline, output_dir=base)
    written = 0
    for short in shorts:
        slug = _build_short_slug(short)
        short_dir = base / f"shorts-{slug}"
        short_dir.mkdir(parents=True, exist_ok=True)
        (short_dir / "short_timeline.json").write_text(
            json.dumps(short_json_payload(timeline, short), indent=2), encoding="utf-8")
        written += 1
    print(f"[OK] {len(shorts)} shorts -> {written} file(s) under {base}{suffix}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("demo_path")
    ap.add_argument("--player", default="",
                    help="POV player nickname (single POV, not whole match)")
    ap.add_argument("--map", default="", help="Map display name (auto-detected if omitted)")
    ap.add_argument("--steam-id", default="",
                    help="POV steam64 (stable; preferred over --player)")
    ap.add_argument("--faceit-id", default="",
                    help="POV FACEIT player id (stable; preferred over --player)")
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

    if not (args.player or args.steam_id or args.faceit_id):
        ap.error("one of --player, --steam-id, --faceit-id is required")

    backlog_file = create_faceit_backlog(
        demo_path=args.demo_path,
        player=args.player,
        map=args.map,
        steam_id=args.steam_id,
        faceit_id=args.faceit_id,
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
