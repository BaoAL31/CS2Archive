"""Unified FACEIT extraction entry point.

Replaces direct use of ``create_faceit_backlog.py``. For a FACEIT demo + POV
player it performs BOTH:

  1. FACEIT backlog extraction  -> single-POV backlog card
     (delegates to ``create_faceit_backlog.create_faceit_backlog``)
  2. Shorts extraction         -> the demo's top verified Allstar clip
     (delegates to ``cs2archive.shorts.extract_top_allstar``; the WIP
     heuristic detector is opt-in via --heuristic-shorts)

Usage:
    python cs2archive/faceit/extract_backlogs.py <demo_path> --player <nick> --map <map>
        [--steam-id <id>] [--priority high|mid|low] [--match-id <id>]
        [--tournament <name>] [--match-date YYYY-MM-DD]
        [--no-elo] [--shorts] [--include-all-players]

``--player`` is the POV player (single POV only — not a whole-match card).
Pass ``--no-shorts`` to skip short extraction.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from cs2archive.faceit.create_faceit_backlog import create_faceit_backlog  # noqa: E402


def _extract_shorts_allstar(demo: Path, player_nick: str = "") -> None:
    """Allstar-anchored nomination: the demo's top verified Allstar clip.

    Default path — one kill-count-verified short per demo, ranked by real
    Allstar views. No heuristic detector output ever ships from here.
    """
    from cs2archive._backlog_common import card_nicks_for_demo, persist_shorts_grouped
    from cs2archive.shorts.extract_top_allstar import extract_top_allstar_short

    try:
        top = extract_top_allstar_short(demo)
    except Exception as e:
        print(f"  [WARN] Allstar nominate failed for {demo.name}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return
    if not top:
        print(f"[OK] no verified Allstar clip for {demo.name} — no short")
        return
    try:
        from cs2archive.shorts.detect_team import detect_pov_opponent
        pov, opp = detect_pov_opponent(demo, top["pov_steam_id"])
        if pov:
            top["pov_team"] = pov
        if opp:
            top["opponent"] = opp
    except Exception:
        pass
    timeline = {"demo_path": str(demo), "map": top.get("map"),
                "shorts": [], "source": "allstar_top"}
    nick = player_nick or ""
    if not nick:
        nick = card_nicks_for_demo(demo).get(str(top.get("pov_steam_id")), "")
    print(f"  [SHORTS] top Allstar = {top.get('pov_nick')} "
          f"{top.get('short_type')} R{top.get('round')} "
          f"{(top.get('allstar') or {}).get('views')} views "
          f"({(top.get('allstar') or {}).get('confidence')})")
    written = persist_shorts_grouped(demo, timeline, [top],
                                     {str(top.get("pov_steam_id")): nick} if nick else None)
    print(f"[OK] {written} short -> POV shorts/ dir")


def _extract_shorts_heuristic(demo: Path, steam_id: str, include_all_players: bool,
                              player_nick: str = "") -> None:
    """WIP heuristic detector (4K/clutch/wallbang/...). Opt-in only via
    --heuristic-shorts; NOT the shipping path — it emits unverified cuts
    that never went through the Allstar picker (see render_match_top.py)."""
    from cs2archive.shorts.build_short_timeline import build_short_timeline
    from cs2archive._backlog_common import persist_shorts_grouped

    pros_only = not include_all_players
    timeline = build_short_timeline(demo, player=steam_id, pros_only=pros_only)
    dropped_randos = timeline.get("_dropped_randos", 0)
    dropped_demand = 0
    shorts = timeline.get("shorts", [])
    if pros_only:
        from cs2archive.shorts.demand_gate import filter_publishable_shorts, folder_orgs
        shorts, dropped_demand = filter_publishable_shorts(
            shorts, orgs=folder_orgs(demo), source="faceit",
        )
    from cs2archive.shorts.demand_gate import filter_suffix
    suffix = filter_suffix(dropped_randos, dropped_demand, source="faceit")
    if not shorts:
        print(f"[OK] 0 shorts detected{suffix}")
        return
    written = persist_shorts_grouped(
        demo, timeline, shorts,
        {steam_id: player_nick} if steam_id and player_nick else None)
    print(f"[OK] {len(shorts)} shorts -> {written} file(s) under POV shorts/ dirs{suffix}")


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
    ap.add_argument("--shorts", action="store_true",
                    help="Enable Shorts extraction (top verified Allstar clip "
                         "for the demo; off by default)")
    ap.add_argument("--heuristic-shorts", action="store_true",
                    help="WIP: use the heuristic detector instead of the "
                         "Allstar nomination (unverified cuts — not shipping)")
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

    if not args.shorts:
        print("[SKIP] shorts extraction disabled (default off; pass --shorts to enable)")
        return

    meta = json.loads(Path(backlog_file).read_text(encoding="utf-8"))
    steam_id = meta.get("steam_id") or args.steam_id
    if not steam_id:
        print("[WARN] no steam_id available; skipping shorts extraction")
        return
    print("[SHORTS] Extracting top Allstar short ...")
    demo = Path(args.demo_path).resolve()
    nick = str(meta.get("player") or args.player or "")
    if args.heuristic_shorts:
        print("[SHORTS] --heuristic-shorts set: using WIP heuristic detector "
              "(unverified cuts, not the shipping path)")
        _extract_shorts_heuristic(demo, steam_id, args.include_all_players,
                                  player_nick=nick)
        return
    _extract_shorts_allstar(demo, player_nick=nick)


if __name__ == "__main__":
    main()
