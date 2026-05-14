"""
CS2Archive — Pipeline Test

Runs the full pipeline on trending match demos:
1. Scrape HLTV ratings → JSON
2. Scrape player profile pictures → local files
3. Analyze demos via CS2DM CLI → JSON stats
4. Verify everything is present
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from config import settings
from models import DemoSource, MatchInfo

console = Console(force_terminal=True)

ANALYSIS_DIR = settings.demo_storage_dir / "analysis"
AVATAR_DIR = settings.demo_storage_dir / "avatars"
CSDA = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\resources\static\csda.exe"

MATCHES = [
    {
        "name": "TheMongolz vs Falcons",
        "hltv_url": "https://www.hltv.org/matches/2394142/the-mongolz-vs-falcons-pgl-astana-2026",
        "slug": "the-mongolz-vs-falcons",
        "demo_files": [
            "demos/hltv/the-mongolz-vs-falcons/the-mongolz-vs-falcons-m1-dust2.dem",
            "demos/hltv/the-mongolz-vs-falcons/the-mongolz-vs-falcons-m2-inferno.dem",
            "demos/hltv/the-mongolz-vs-falcons/the-mongolz-vs-falcons-m3-mirage.dem",
        ],
    },
    {
        "name": "BetBoom vs Vitality",
        "hltv_url": "https://www.hltv.org/matches/2394157/betboom-vs-vitality-iem-atlanta-2026",
        "slug": "betboom-vs-vitality",
        "demo_files": [
            "demos/hltv/betboom-vs-vitality/betboom-vs-vitality-m1-anubis.dem",
            "demos/hltv/betboom-vs-vitality/betboom-vs-vitality-m2-overpass.dem",
            "demos/hltv/betboom-vs-vitality/betboom-vs-vitality-m3-nuke.dem",
        ],
    },
    {
        "name": "B8 vs BC.Game",
        "hltv_url": "https://www.hltv.org/matches/2394159/b8-vs-bcgame-iem-atlanta-2026",
        "slug": "b8-vs-bcgame",
        "demo_files": [
            "demos/hltv/b8-vs-bcgame/b8-vs-bc-game-m1-overpass.dem",
            "demos/hltv/b8-vs-bcgame/b8-vs-bc-game-m2-mirage.dem",
        ],
    },
]


async def run_pipeline() -> None:
    """Run the full test pipeline on all matches."""
    console.print(Panel("[bold cyan]CS2Archive Pipeline Test[/bold cyan]"))

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    results = []

    for match in MATCHES:
        console.print(f"\n[bold]━━━ {'='*60}[/bold]")
        console.print(f"[bold cyan]Testing:[/bold cyan] {match['name']}")
        console.print(f"[dim]{match['hltv_url']}[/dim]")

        match_result = {
            "match": match["name"],
            "url": match["hltv_url"],
            "ratings": None,
            "avatars": None,
            "csdm_analysis": None,
            "demos_present": False,
            "errors": [],
        }

        # 1. Scrape HLTV ratings
        console.print(f"\n[bold]Step 1:[/bold] Scrape HLTV ratings...")
        try:
            from scrapers.ratings import get_match_ratings

            ratings = await get_match_ratings(match["hltv_url"])
            if ratings and ratings.get("tables"):
                ratings_path = ANALYSIS_DIR / f"{match['slug']}_ratings.json"
                d = {"match": match["name"], "url": match["hltv_url"], "tables": []}
                for t in ratings["tables"]:
                    d["tables"].append({
                        "team": t.get("team", ""),
                        "map": t.get("map", ""),
                        "players": [
                            {"nickname": p["nickname"], "kd": p["kd"], "swing": p["swing"],
                             "adr": p["adr"], "kast": p["kast"], "rating": p["rating"]}
                            for p in t["players"]
                        ],
                    })
                ratings_path.write_text(json.dumps(d, indent=2), encoding="utf-8")
                match_result["ratings"] = str(ratings_path)
                player_count = sum(len(t["players"]) for t in d["tables"])
                console.print(f"[green]   [OK] {len(d['tables'])} team tables, ~{player_count} player entries[/green]")
            else:
                match_result["errors"].append("No ratings found")
                console.print("[red]   [FAIL] No ratings data[/red]")
        except Exception as e:
            match_result["errors"].append(f"Ratings scrape error: {e}")
            console.print(f"[red]   [FAIL] {e}[/red]")

        # 2. Scrape player avatars
        console.print(f"\n[bold]Step 2:[/bold] Scrape player avatars...")
        try:
            from scrapers.player_images import get_player_avatars

            avatars = await get_player_avatars(match["hltv_url"])
            if avatars:
                match_result["avatars"] = len(avatars)
                console.print(f"[green]   [OK] {len(avatars)} avatars saved[/green]")
            else:
                match_result["errors"].append("No avatars found")
                console.print("[yellow]   No avatars found[/yellow]")
        except Exception as e:
            match_result["errors"].append(f"Avatar error: {e}")
            console.print(f"[red]   [FAIL] {e}[/red]")

        # 3. Check demo files exist
        console.print(f"\n[bold]Step 3:[/bold] Check demo files on disk...")
        demo_list = []
        for df in match["demo_files"]:
            p = Path(df)
            if p.exists() and p.stat().st_size > 1024:
                sz = p.stat().st_size / 1024 / 1024
                demo_list.append({"path": str(p), "size_mb": round(sz, 1)})
                console.print(f"[green]   [OK] {p.name} ({sz:.1f} MB)[/green]")
            else:
                demo_list.append({"path": str(p), "size_mb": 0})
                console.print(f"[yellow]   [MISS] {p.name}[/yellow]")

        match_result["demos"] = demo_list
        match_result["demos_present"] = all(d["size_mb"] > 0 for d in demo_list)
        if not match_result["demos_present"]:
            match_result["errors"].append("Missing demo files")

        # 4. Analyze via CS2DM csda.exe
        console.print(f"\n[bold]Step 4:[/bold] Analyze via CS2DM (csda)...")
        demos_analyzed = 0
        for demo_info in demo_list:
            if demo_info["size_mb"] == 0:
                continue
            demo_path = demo_info["path"]
            basename = Path(demo_path).stem
            output_path = ANALYSIS_DIR / f"{match['slug']}_{basename}_csdm.json"
            if output_path.exists():
                demos_analyzed += 1
                continue
            try:
                result = subprocess.run(
                    [CSDA, "-demo-path", os.path.abspath(demo_path),
                     "-format", "json", "-output", os.path.abspath(str(ANALYSIS_DIR))],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    demos_analyzed += 1
                    console.print(f"[green]   [OK] {basename}.dem analyzed[/green]")
                else:
                    console.print(f"[yellow]   [SKIP] {basename}.dem: csda returned {result.returncode}[/yellow]")
            except subprocess.TimeoutExpired:
                console.print(f"[yellow]   [SKIP] {basename}.dem: csda timed out[/yellow]")
            except FileNotFoundError:
                console.print("[yellow]   csda.exe not found, skipping analysis[/yellow]")
                break
            except Exception as e:
                console.print(f"[yellow]   [{basename}] {e}[/yellow]")

        match_result["csdm_analysis"] = demos_analyzed
        if demos_analyzed > 0:
            console.print(f"[green]   [OK] {demos_analyzed}/{len([d for d in demo_list if d['size_mb'] > 0])} demos analyzed[/green]")
        else:
            match_result["errors"].append("No CS2DM analysis results")

        results.append(match_result)

    # Summary
    console.print(f"\n\n[bold cyan]{'='*70}[/bold cyan]")
    console.print(f"[bold cyan]PIPELINE TEST SUMMARY[/bold cyan]")
    console.print(f"[bold cyan]{'='*70}[/bold cyan]")

    total = len(results)
    passed = sum(1 for r in results if not r["errors"])
    failed = total - passed

    table = Table(title=f"Results: {passed}/{total} passed", show_lines=True)
    table.add_column("Match", style="cyan", width=28)
    table.add_column("Ratings", width=8)
    table.add_column("Avatars", width=8)
    table.add_column("Demos", width=8)
    table.add_column("CS2DM", width=8)
    table.add_column("Status", width=8)

    for r in results:
        status = "[red]FAIL[/red]" if r["errors"] else "[green]PASS[/green]"
        ratings = "[green]OK[/green]" if r["ratings"] else "[red]—[/red]"
        avatars = f"[green]{r['avatars']}[/green]" if r["avatars"] else "[red]—[/red]"
        demos = "[green]OK[/green]" if r["demos_present"] else "[red]MISS[/red]"
        csdm = f"[green]{r['csdm_analysis']}[/green]" if r.get("csdm_analysis", 0) > 0 else "[red]—[/red]"
        table.add_row(r["match"][:26], ratings, avatars, demos, csdm, status)

    console.print()
    console.print(table)

    if passed < total:
        for r in results:
            if r["errors"]:
                console.print(f"\n[red]{r['match']}[/red] errors:")
                for e in r["errors"]:
                    console.print(f"  [dim]• {e}[/dim]")

    console.print(f"\n[bold]Output directories:[/bold]")
    console.print(f"  Ratings:  file:///{ANALYSIS_DIR.resolve().as_posix()}")
    console.print(f"  Avatars:  file:///{AVATAR_DIR.resolve().as_posix()}")
    console.print(f"  Demos:    file:///{(settings.demo_storage_dir / 'hltv').resolve().as_posix()}")
