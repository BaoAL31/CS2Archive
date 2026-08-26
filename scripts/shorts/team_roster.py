"""Persistent team-roster tracker.

Keeps ``.data/team_roster.json`` in sync with what the demos actually say,
so we always have an authoritative player -> team map without re-parsing or
trusting memory. Updated every time a demo is extracted/backlogged.

Structure:
    {
      "players": {
        "<steam_id>": {
          "nickname": "...",
          "teams": {"Spirit": 12, "FUT": 1},   # times seen on each org
          "current_team": "Spirit"              # argmax, ties -> latest demo
        }
      },
      "teams": {
        "Spirit": ["765...", "765..."],
        "FUT":    ["765..."]
      }
    }

Usage:
    python scripts/shorts/team_roster.py --demo demos/hltv/<slug>/<file>.dem
    python scripts/shorts/team_roster.py --backfill demos/hltv   # all .dem
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

DATA = PROJECT_ROOT / ".data" / "team_roster.json"


def _load() -> dict:
    if DATA.exists():
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"players": {}, "teams": {}}


def _save(data: dict) -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(data, indent=1), encoding="utf-8")


def update_team_roster(demo_path: str, demo_tag: str | None = None) -> dict:
    """Parse one demo and merge its roster into the persistent tracker.

    Returns the (mutated) data dict. ``demo_tag`` is stored as ``last_demo``
    per player for tie-breaking; defaults to the demo file name.
    """
    from scripts.shorts.detect_team import org_per_player

    # Never derive teams from FACEIT pug demos (fake 'Team_X' orgs).
    if any(seg in str(demo_path).lower() for seg in ("faceit", "utility_cams", "workdir")):
        return _load()

    tag = demo_tag or Path(demo_path).name
    roster = org_per_player(demo_path)
    if not roster:
        return _load()

    data = _load()
    players = data["players"]
    teams = data.setdefault("teams", {})

    for sid, (nick, org) in roster.items():
        p = players.setdefault(sid, {"nickname": nick, "teams": {}, "current_team": org})
        p["nickname"] = nick
        p["teams"][org] = p["teams"].get(org, 0) + 1
        p["current_team"] = org
        p["last_demo"] = tag
        teams.setdefault(org, [])
        if sid not in teams[org]:
            teams[org].append(sid)

    _save(data)
    return data


def _cli() -> None:
    import argparse
    import glob as _glob

    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", help="a single .dem to ingest")
    ap.add_argument("--backfill", help="folder to scan for all *.dem")
    args = ap.parse_args()

    if args.demo:
        update_team_roster(args.demo)
        print(f"[OK] ingested {Path(args.demo).name}")
    elif args.backfill:
        count = 0
        for f in sorted(Path(args.backfill).rglob("*.dem")):
            # Skip FACEIT pug demos (fake 'Team_X' orgs) and render workdirs.
            if any(seg in str(f) for seg in ("faceit", "utility_cams", "workdir", "cs2util")):
                continue
            try:
                update_team_roster(str(f))
                count += 1
            except Exception as e:
                print(f"[WARN] {f.name}: {e}")
        print(f"[OK] backfilled {count} demos")
    else:
        ap.print_usage()


if __name__ == "__main__":
    _cli()