"""Generate a FACEIT POV thumbnail (blurred kill-frame + text, no avatar/ratings).

The individual FACEIT thumbnail shows player, match ELO ("3521 ELO" /
"vs 3105 ELO") when available, and map — no team names, tournament, or stage.

Usage:
    python scripts/faceit/faceit_thumbnail.py <demo_path> --player <nick> --map <map>
                                  [--steam-id <id>] [--elo <int>] [--opp-elo <int>]
                                  [--variant raw|overlay] --output <youtube_dir>

Requires CS2DemoManager (csdm) for kill-frame extraction, same as the HLTV
thumbnail path (thumbnail.cli.extract_background_frame).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()
sys.path.insert(0, str(PROJECT_ROOT / "thumbnail"))

from thumbnail.cli import extract_background_frame  # noqa: E402
from thumbnail.layouts import generate_faceit  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo_path", help="FACEIT .dem path")
    ap.add_argument("--player", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--steam-id", default="")
    ap.add_argument("--elo", type=int, default=None, help="POV player's FACEIT ELO")
    ap.add_argument("--opp-elo", type=int, default=None, help="Average FACEIT ELO of the opposing team")
    ap.add_argument("--kd", default=None, help="K/D line for the POV player, e.g. '38/9'")
    ap.add_argument("--background", default=None,
                    help="Reuse a cached background frame (jpg) instead of rendering a new kill frame")
    ap.add_argument("--variant", choices=["raw", "overlay"], default="raw")
    ap.add_argument("--output", required=True, help="youtube dir to write thumbnail.jpg")
    args = ap.parse_args()

    # Canonicalize player name (proper casing: NiKo, TeSeS, ...)
    from faceit_names import canonical_nick, avatar_path
    player = canonical_nick(args.player)
    av_path = avatar_path(args.player)
    if av_path is None:
        print(f"  [WARN] No avatar for {player}")

    demo = Path(args.demo_path)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.background:
        bg = Path(args.background)
    elif args.steam_id:
        bg = extract_background_frame(str(demo), args.steam_id)
    else:
        bg = None
    if bg is None or not Path(bg).exists():
        # fallback: solid dark bg
        from PIL import Image
        bg = PROJECT_ROOT / "tmp" / "faceit_bg_fallback.png"
        Image.new("RGB", (1280, 720), (20, 20, 24)).save(bg)

    img = generate_faceit(
        bg, player, args.map, args.elo, args.opp_elo,
        kd=(args.kd or "").replace("/", "-"),
        variant=args.variant, avatar_path=av_path,
    )
    img = img.convert("RGB")
    out = out_dir / "thumbnail.jpg"
    img.save(out, "JPEG", quality=95, subsampling=0)
    print(f"[OK] FACEIT thumbnail: {out}")


if __name__ == "__main__":
    main()
