from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP_DIR = Path(os.environ.get("TMPDIR", "C:/Users/jembo/AppData/Local/Temp/opencode"))
TMP_DIR.mkdir(parents=True, exist_ok=True)

from rich.console import Console

from thumbnail.layouts import generate
from thumbnail.utils import (
    YOUTUBE_DIR,
    find_player_stats,
    find_ratings_file,
    get_avatar_path,
    get_slug_from_url,
    get_team_names,
    load_ratings,
)

console = Console(force_terminal=True)
CSDM = r"C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd"


def resolve_ratings(input_arg: str) -> tuple[Path, str]:
    p = Path(input_arg)
    if p.exists() and p.suffix == ".json":
        return p, p.stem.replace("_ratings", "")

    slug = get_slug_from_url(input_arg)
    if slug:
        found = find_ratings_file(slug)
        if found:
            return found, slug

    for f in sorted(Path("demos/analysis").glob("*_ratings.json"), reverse=True):
        data = load_ratings(f)
        if input_arg in data.get("url", "") or input_arg in data.get("match", ""):
            return f, data.get("match", f.stem.replace("_ratings", ""))

    raise FileNotFoundError(f"Could not resolve ratings file for: {input_arg}")


def build_output_dir(match_slug: str, player: str, map_name: str) -> Path:
    safe_player = player.lower().replace(" ", "_")
    safe_map = map_name.lower().replace(" ", "_")
    out = YOUTUBE_DIR / f"{match_slug}_{safe_player}_{safe_map}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def extract_background_frame(demo_path: str, steam_id: str) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="thumb_bg_", dir=TMP_DIR))
    try:
        console.print(f"  Finding kills for player...")
        subprocess.run(
            [CSDM, "json", str(demo_path), "--output-folder", str(tmp_dir)],
            capture_output=True, text=True, timeout=300,
        )

        json_files = list(tmp_dir.glob("*.json"))
        if not json_files:
            console.print("[red]  Failed to export demo data[/red]")
            sys.exit(1)

        data = json.loads(json_files[0].read_text())
        kills = [
            k for k in data.get("kills", [])
            if k.get("killerSteamId") == steam_id
        ]

        if not kills:
            console.print(f"[red]  No kills found for steam ID {steam_id}[/red]")
            sys.exit(1)

        kill = random.choice(kills)
        tick = int(kill["tick"])
        tickrate = int(data.get("tickrate", 64))
        before = int(tickrate * 1)
        start_tick = max(0, tick - before)
        end_tick = tick

        console.print(f"  Rendering kill at tick {tick} ({kill.get('weaponName', '?')}, {kill.get('victimName', '?')})...")

        cmd = [
            CSDM, "video", str(demo_path),
            str(start_tick), str(end_tick),
            "--focus-player", steam_id,
            "--perspective", "player",
            "--no-show-x-ray",
            "--no-show-only-death-notices",
            "--output", str(tmp_dir),
            "--width", "1920",
            "--height", "1080",
            "--framerate", "30",
            "--recording-system", "CS",
            "--close-game-after-recording",
            "--cfg", "assets/cs2_pov.cfg",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            console.print(f"[red]  Clip render failed: {result.stderr[:200]}[/red]")
            sys.exit(1)

        clips = sorted(tmp_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        if not clips:
            console.print("[red]  No clip generated[/red]")
            sys.exit(1)

        clip = clips[-1]
        frame_path = tmp_dir / "bg_frame.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(clip), "-vframes", "1", "-update", "1", str(frame_path)],
            capture_output=True, text=True, timeout=30,
        )

        if not frame_path.exists():
            console.print("[red]  Frame extraction failed[/red]")
            sys.exit(1)

        dest = YOUTUBE_DIR / "bg_frame.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(frame_path), str(dest))
        return dest

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CS2 POV thumbnail")
    parser.add_argument("input", help="Match URL or ratings JSON path")
    parser.add_argument("--player", "-p", required=True, help="Player nickname")
    parser.add_argument("--map", "-m", required=True, help="Map name (e.g. Mirage, Nuke)")
    parser.add_argument("--background", "-b", help="Path to background frame (omit to auto-extract from demo)")
    parser.add_argument("--demo", help="Path to .dem file (for auto background extraction)")
    parser.add_argument("--steam-id", help="Player Steam64 ID (required with --demo)")
    parser.add_argument("--tournament", "-t", help="Tournament name (e.g. IEM Atlanta 2026)")
    parser.add_argument("--output", "-o", help="Output path (defaults to youtube/...)")
    args = parser.parse_args()

    console.print("[bold cyan]=== Thumbnail Generator ===[/bold cyan]")

    ratings_path, match_slug = resolve_ratings(args.input)
    console.print(f"[dim]  Ratings: {ratings_path.name}[/dim]")

    ratings = load_ratings(ratings_path)
    player_info = find_player_stats(ratings, args.player, args.map)
    if not player_info:
        console.print(f"[red]  Player '{args.player}' not found on {args.map}[/red]")
        return

    team1, team2 = get_team_names(ratings, args.map)

    avatar_path = get_avatar_path(args.player)
    if not avatar_path:
        console.print(f"[red]  No avatar found for '{args.player}' in demos/avatars/[/red]")
        return

    if args.background:
        bg_path = Path(args.background)
        if not bg_path.exists():
            console.print(f"[red]  Background frame not found: {bg_path}[/red]")
            return
    elif args.demo and args.steam_id:
        if not Path(args.demo).exists():
            console.print(f"[red]  Demo not found: {args.demo}[/red]")
            return
        bg_path = extract_background_frame(args.demo, args.steam_id)
    else:
        console.print("[red]  Provide --background or --demo + --steam-id[/red]")
        return

    console.print(f"  Player: [cyan]{args.player}[/cyan]")
    console.print(f"  Map:    [cyan]{args.map}[/cyan]")
    console.print(f"  K-D:    [green]{player_info['kd']}[/green]")
    console.print(f"  Rating: [green]{player_info['rating']}[/green]")
    stage = ratings.get("match_stage", "")
    console.print(f"  Match:  [cyan]{team1} vs {team2}[/cyan]")
    if stage:
        console.print(f"  Stage:  [cyan]{stage}[/cyan]")
    console.print(f"  Avatar: [dim]{avatar_path.name}[/dim]")
    console.print(f"  BG:     [dim]{bg_path.name}[/dim]")

    output_dir = Path(args.output) if args.output else build_output_dir(match_slug, args.player, args.map)
    output_path = output_dir / "thumbnail.png"

    console.print(f"\n[bold]Generating thumbnail -> {output_path}[/bold]")

    img = generate(
        bg_path,
        avatar_path,
        args.player,
        player_info["kd"],
        player_info["rating"],
        args.map,
        f"{team1} vs {team2}",
        tournament=args.tournament or "",
        stage=stage,
    )
    img.save(output_path, "PNG")

    # Clean up auto-extracted bg frame
    if not args.background and bg_path.parent == YOUTUBE_DIR:
        bg_path.unlink(missing_ok=True)

    console.print("\n[green]Done![/green]")
