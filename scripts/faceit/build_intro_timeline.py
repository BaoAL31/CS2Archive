"""Build an Intro Timeline: pick the single most impressive moment of a demo.

Reuses the shorts extractor's detection (``scripts/shorts/build_short_timeline.py``)
then applies the intro ranking:

  - Qualifying moments: **1v3+ clutches** (1v3 / 1v4 / 1v5 won rounds) and
    **5k multikills** (5 kills by the same attacker = ACE).
  - Rank: clutches first (bigger disadvantage 1v5 > 1v4 > 1v3, then more kills),
    then 5k multikills.
  - Pick exactly ONE moment. The final intro length cap (60s) is enforced at
    render time — this builder keeps the full short window (all kills intact).

Usage:
    python scripts/faceit/build_intro_timeline.py demos/faceit/<demo>.dem
    python scripts/faceit/build_intro_timeline.py demos/faceit/<demo>.dem --output renders/hl-<stem>/intro/intro_timeline.json
    python scripts/faceit/build_intro_timeline.py demos/faceit/<demo>.dem --player 76561198074762801
    python scripts/faceit/build_intro_timeline.py demos/faceit/<demo>.dem --include-all-players

Output:
    renders/hl-{demo_stem}/intro/intro_timeline.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from shorts.build_short_timeline import build_short_timeline  # noqa: E402

VALID_CLUTCH_COUNTS = ("1v3", "1v4", "1v5")


def _clutch_enemy_count(short: dict) -> int:
    """Enemy side of a clutch (e.g. "1v4" -> 4). Non-clutch -> 0."""
    cnt = short.get("clutch_initial_count", "")
    try:
        return int(cnt.split("v")[1])
    except (ValueError, IndexError):
        return 0


def _rank_key(short: dict):
    """Max-sort key: clutches (priority 1) before 5k multikills (priority 0).

    Clutch: enemy count desc, then kills desc. 5k: kills desc.
    """
    if short.get("short_type") == "clutch":
        return (
            1,
            _clutch_enemy_count(short),
            len(short.get("kill_ticks", [])),
        )
    return (
        0,
        len(short.get("kill_ticks", [])),
        0,
    )


def _moment_label(short: dict) -> str:
    if short.get("short_type") == "clutch":
        cnt = short.get("clutch_initial_count", "1v3")
        return f"{cnt.upper()} CLUTCH"
    return "5K"


def _kind(short: dict) -> str:
    return "clutch" if short.get("short_type") == "clutch" else "multikill"


def _rank_reason(short: dict) -> str:
    kills = len(short.get("kill_ticks", []))
    if short.get("short_type") == "clutch":
        cnt = short.get("clutch_initial_count", "1v3")
        return f"clutch {cnt}, {kills} kill(s)"
    return f"5k multikill, {kills} kill(s)"


def build_intro_timeline(
    demo_path: Path,
    pros_only: bool = True,
    player: str | None = None,
) -> dict:
    """Rank a demo's 1v3+ clutches and 5k multikills and pick the best one."""
    demo_path = Path(demo_path)
    timeline = build_short_timeline(demo_path, pros_only=pros_only, player=player)
    shorts = timeline.get("shorts", [])

    if player:
        player = str(player).strip()
        shorts = [
            s for s in shorts
            if str(s.get("pov_steam_id", "")) == player
            or (s.get("pov_nick") or "").strip().lower() == player.lower()
        ]

    qualifying = [
        s for s in shorts
        if (
            s.get("short_type") == "clutch"
            and s.get("clutch_initial_count") in VALID_CLUTCH_COUNTS
        )
        or (
            s.get("short_type") == "4k"
            and len(s.get("kill_ticks", [])) >= 5
        )
    ]
    if not qualifying:
        return {
            "intro_type": "intro_timeline",
            "demo_path": str(demo_path),
            "map": timeline.get("map", "Unknown"),
            "picked": None,
            "candidates": [],
            "reason": "no qualifying moments (1v3+ clutch or 5k multikill)",
        }

    ranked = sorted(qualifying, key=_rank_key, reverse=True)
    best = dict(ranked[0])

    # Keep the FULL short window (lead-in + all kills + win buffer). The 60s
    # cap is enforced during assembly: render_intro crossfades away the >=15s
    # dead stretches and trims the head only if the edited result still exceeds
    # 60s (keeping the climactic ending). Capping the source window here would
    # drop earlier kills of a spread-out multikill/clutch.
    start_tick = int(best["start_tick"])
    end_tick = int(best["end_tick"])
    best["start_tick"] = start_tick
    best["end_tick"] = end_tick
    best["kill_ticks"] = [
        int(k) for k in best.get("kill_ticks", [])
        if start_tick <= int(k) <= end_tick
    ]

    picked = {
        "kind": _kind(best),
        "label": _moment_label(best),
        "pov_steam_id": str(best.get("pov_steam_id", "")),
        "pov_nick": best.get("pov_nick", "Unknown"),
        "start_tick": start_tick,
        "end_tick": end_tick,
        "kill_ticks": best["kill_ticks"],
        "clutch_initial_count": best.get("clutch_initial_count", ""),
        "round_win_tick": best.get("round_win_tick"),
        "win_event": best.get("win_event"),
        "rank_reason": _rank_reason(best),
    }

    candidates = []
    for s in ranked:
        candidates.append({
            "kind": _kind(s),
            "label": _moment_label(s),
            "pov_steam_id": str(s.get("pov_steam_id", "")),
            "pov_nick": s.get("pov_nick", "Unknown"),
            "start_tick": int(s["start_tick"]),
            "end_tick": int(s["end_tick"]),
            "kill_ticks": [int(k) for k in s.get("kill_ticks", [])],
            "clutch_initial_count": s.get("clutch_initial_count", ""),
            "rank_reason": _rank_reason(s),
        })

    return {
        "intro_type": "intro_timeline",
        "demo_path": str(demo_path),
        "map": timeline.get("map", "Unknown"),
        "picked": picked,
        "candidates": candidates,
        "reason": picked["rank_reason"],
    }


def intro_run_dir(demo_path: Path) -> Path:
    return (_PROJECT_ROOT / "renders" / f"hl-{Path(demo_path).stem}" / "intro")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Intro Timeline JSON from a demo")
    ap.add_argument("demo_path", type=Path, help="Path to .dem file")
    ap.add_argument("--output", "-o", type=Path, default=None,
                    help="Override output JSON path (default: renders/hl-{stem}/intro/intro_timeline.json)")
    ap.add_argument("--include-all-players", action="store_true",
                    help="Keep moments for any player (default: only Recognised Pros "
                         "from .data/player_accounts.json)")
    ap.add_argument("--player", type=str, default=None,
                    help="Only consider this player's moments (steam64 or nickname)")
    args = ap.parse_args()

    demo = args.demo_path
    if not demo.is_file():
        print(f"[ERR] demo not found: {demo}", file=sys.stderr)
        return 1

    timeline = build_intro_timeline(
        demo, pros_only=not args.include_all_players, player=args.player,
    )

    if timeline.get("picked") is None:
        print(f"[OK] {timeline['reason']} (no output written)")
        return 0

    out = args.output or (intro_run_dir(demo) / "intro_timeline.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    p = timeline["picked"]
    print(f"[OK] picked: {p['pov_nick']} | {p['label']} | {timeline['map']} | "
          f"{p['start_tick']}->{p['end_tick']} | {len(p['kill_ticks'])} kills")
    print(f"     {len(timeline['candidates'])} candidate(s), ranked -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
