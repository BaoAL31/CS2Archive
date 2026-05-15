"""
Show the highest-rated player from each team on each map.

Usage:
    python scripts/best_per_map.py <ratings_json>
    python scripts/best_per_map.py demos/analysis/faze-vs-vitality-iem-atlanta-2026_ratings.json

Example output:
    Nuke      FaZe          Twistzz   31-24  1.52
    Nuke      Vitality      ropz      27-18  1.54
    Dust2     FaZe          Neityu    16-14  1.46
    Dust2     Vitality      ZywOo     22-9   1.86
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <ratings_json>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"{'Map':10s} {'Team':14s} {'Player':12s} {'K-D':7s} {'Rating'}")
    print("-" * 55)

    seen_maps: set[str] = set()
    for table in data.get("tables", []):
        m = table["map"]
        if m == "Series Overall":
            continue
        best = max(table["players"], key=lambda x: float(x["rating"]))
        print(f"{m:10s} {table['team']:14s} {best['nickname']:12s} {best['kd']:7s} {best['rating']}")


if __name__ == "__main__":
    main()
