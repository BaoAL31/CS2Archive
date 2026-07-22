"""Create a backlog entry for an already-downloaded FACEIT demo.

The demo must live under demos/faceit/ (or any path). The resulting backlog
entry carries is_faceit=true so the pipeline uses the FACEIT title/thumbnail
path automatically.

Usage:
    python scripts/faceit/create_faceit_backlog.py <demo_path> --player <nick> --map <map>
                                  [--steam-id <id>] [--tournament <name>] [--priority high]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

BACKLOG_DIR = PROJECT_ROOT / "backlog"
MAP_DISPLAY = {
    "de_ancient": "Ancient", "de_mirage": "Mirage", "de_inferno": "Inferno",
    "de_nuke": "Nuke", "de_anubis": "Anubis", "de_overpass": "Overpass",
    "de_vertigo": "Vertigo", "de_dust2": "Dust2", "de_train": "Train",
}


def _resolve_steam_id(demo_path: Path, nick: str) -> str:
    try:
        import demoparser2 as dp
        info = dp.DemoParser(str(demo_path)).parse_player_info()
        for _, row in info.iterrows():
            if str(row.get("name", "")).strip().lower() == nick.lower():
                return str(row.get("steamid", "")).strip()
    except Exception as e:
        print(f"  [WARN] steam id resolve failed: {e}")
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path")
    ap.add_argument("--player", required=True)
    ap.add_argument("--map", default="")
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--tournament", default="")
    ap.add_argument("--priority", default="high", choices=["high", "mid", "low"])
    args = ap.parse_args()

    demo = Path(args.demo_path).resolve()
    if not demo.exists():
        print(f"[ERR] demo not found: {demo}")
        sys.exit(1)

    demo_rel = str(demo.relative_to(PROJECT_ROOT)).replace("\\", "/")
    map_name = args.map
    if not map_name:
        m = re.search(r"(de_[a-z0-9]+)", demo.name, re.I)
        if m:
            map_name = MAP_DISPLAY.get(m.group(1).lower(), m.group(1).replace("de_", "").capitalize())

    steam_id = args.steam_id or _resolve_steam_id(demo, args.player)

    slug = f"{args.player.lower()}-{map_name.lower()}-{demo.stem}"
    backlog_dir = BACKLOG_DIR / "faceit" / args.priority
    backlog_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = backlog_dir / f"{slug}.json"

    meta = {
        "player": args.player,
        "map": map_name,
        "steam_id": steam_id,
        "demo_path": demo_rel,
        "tournament": args.tournament,
        "priority": args.priority,
        "is_faceit": True,
        "pipeline_cmd": (
            f'$env:PYTHONPATH=.; & C:/Users/jembo/anaconda3/envs/cs2archive/python.exe '
            f'scripts/pov/pipeline.py --backlog {backlog_file.relative_to(PROJECT_ROOT).as_posix()}'
        ),
    }
    backlog_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[OK] Created: {backlog_file}")


if __name__ == "__main__":
    main()
