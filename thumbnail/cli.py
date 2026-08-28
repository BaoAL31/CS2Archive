from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
for _p in (str(_PROJECT_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rich.console import Console

from thumbnail.layouts import generate
from thumbnail.utils import (
    YOUTUBE_DIR,
    extract_killfeed_frame,
    find_player_stats,
    find_ratings_file,
    get_avatar_path,
    get_slug_from_url,
    get_team_names,
    load_ratings,
)

console = Console(force_terminal=True)


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


def extract_background_frame(
    video_path: str,
    steam_id: str,
    *,
    demo_path: str | None = None,
    sidecar: str | None = None,
) -> Path:
    dest = YOUTUBE_DIR / "bg_frame.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame = extract_killfeed_frame(
        video_path, steam_id,
        demo_path=demo_path, sidecar_path=sidecar, dest=dest,
    )
    if frame is None:
        console.print(
            "[red]  Could not map a POV kill onto the video "
            "(need video + round_offsets sidecar + kill timeline)[/red]"
        )
        sys.exit(1)
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CS2 POV thumbnail")
    parser.add_argument("input", help="Match URL or ratings JSON path")
    parser.add_argument("--player", "-p", required=True, help="Player nickname")
    parser.add_argument("--map", "-m", required=True, help="Map name (e.g. Mirage, Nuke)")
    parser.add_argument("--background", "-b", help="Path to background frame (omit to extract from --video)")
    parser.add_argument("--video", help="Finished POV mp4 (overlay/raw) to grab the killfeed frame from")
    parser.add_argument("--sidecar", help="round_offsets JSON (defaults to <video-stem>.round_offsets.json)")
    parser.add_argument("--demo", help="Demo path — used only to find the shorts kill timeline")
    parser.add_argument("--steam-id", help="Player Steam64 ID (required with --video)")
    parser.add_argument("--tournament", "-t", help="Tournament name (e.g. IEM Atlanta 2026)")
    parser.add_argument("--tournament-logo", help="Path to a logo image to draw over the tournament name")
    parser.add_argument(
        "--variant",
        choices=["raw", "overlay"],
        default="raw",
        help="Variant: 'raw' (default) or 'overlay' (adds W/ INPUT OVERLAY and + UTIL CAMS badges)",
    )
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
        sys.exit(1)

    if args.background:
        bg_path = Path(args.background)
        if not bg_path.exists():
            console.print(f"[red]  Background frame not found: {bg_path}[/red]")
            return
    elif args.video and args.steam_id:
        if not Path(args.video).exists():
            console.print(f"[red]  Video not found: {args.video}[/red]")
            return
        bg_path = extract_background_frame(
            args.video, args.steam_id,
            demo_path=args.demo, sidecar=args.sidecar,
        )
    else:
        console.print("[red]  Provide --background or --video + --steam-id[/red]")
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
        variant=args.variant,
        tournament_logo=Path(args.tournament_logo) if args.tournament_logo else None,
    )
    img = img.convert("RGB")
    img.save(output_path.with_suffix(".jpg"), "JPEG", quality=95, subsampling=0)
    output_path = output_path.with_suffix(".jpg")

    # Clean up auto-extracted bg frame
    if not args.background and bg_path.parent == YOUTUBE_DIR:
        bg_path.unlink(missing_ok=True)

    console.print("\n[green]Done![/green]")
