"""
Extract all player Steam IDs from a CS2 demo file.

Usage:
    python scripts/extract_steamids.py <demo_path>

Depends on csdm CLI being installed and the demo being analyzed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP_DIR = Path(__file__).resolve().parent.parent / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


def extract_steamids(demo_path: str) -> dict[str, str]:
    demo = Path(demo_path)
    if not demo.exists():
        print(f"[red]Demo not found: {demo}[/red]")
        return {}

    csdm = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"

    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmpdir:
        cmd = [csdm, "json", str(demo), "--output-folder", tmpdir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0 and "unknown demo source" in (result.stderr or "").lower():
            cmd += ["--source", "challengermode"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"[red]csdm json failed: {result.stderr}[/red]")
            return {}

        json_files = list(Path(tmpdir).glob("*.json"))
        if not json_files:
            print("[red]No JSON output from csdm[/red]")
            return {}

        data = json.loads(json_files[0].read_text())
        players: dict[str, str] = {}
        for p in data.get("players", []):
            name = p.get("name", "?")
            sid = p.get("steamId", "")
            if name and sid:
                players[name] = sid
        return players


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <demo_path>")
        sys.exit(1)

    demo_path = sys.argv[1]
    players = extract_steamids(demo_path)

    if not players:
        print("No players found.")
        sys.exit(1)

    print(f"\nPlayers in {Path(demo_path).name}:")
    print(f"{'Name':<20} {'Steam64 ID':<20}")
    print("-" * 40)
    for name in sorted(players.keys()):
        print(f"{name:<20} {players[name]:<20}")


if __name__ == "__main__":
    main()
