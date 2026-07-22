"""Generate a FACEIT POV thumbnail (blurred kill-frame + text, no avatar/ratings).

Usage:
    python scripts/faceit/faceit_thumbnail.py <demo_path> --player <nick> --map <map>
                                  [--steam-id <id>] [--match-detail <text>]
                                  [--tournament <name>] [--variant raw|overlay]
                                  --output <youtube_dir>

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
    ap.add_argument("--match-detail", default="", help="Opponent / team line")
    ap.add_argument("--tournament", default="")
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

    bg = extract_background_frame(str(demo), args.steam_id) if args.steam_id else None
    if bg is None:
        # fallback: solid dark bg
        from PIL import Image
        bg = PROJECT_ROOT / "tmp" / "faceit_bg_fallback.png"
        Image.new("RGB", (1280, 720), (20, 20, 24)).save(bg)

    img = generate_faceit(
        bg, player, args.map, args.match_detail,
        tournament=args.tournament, variant=args.variant,
        avatar_path=av_path,
    )
    img = img.convert("RGB")
    out = out_dir / "thumbnail.jpg"
    img.save(out, "JPEG", quality=95, subsampling=0)
    print(f"[OK] FACEIT thumbnail: {out}")


if __name__ == "__main__":
    main()
