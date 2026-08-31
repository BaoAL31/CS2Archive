"""CLI for style-01 FACEIT thumbnails (HTML study #proof).

Usage:
    python scripts/faceit/render_thumb_01.py --bg <jpg> --avatar <png> \\
        --name donk --score 23-14 --sub "DUO w/ MAGIXX" \\
        --costar <png> --out thumbnail.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()

from thumbnail.proof_01 import BADGE_DEFAULT, render  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Render style-01 (performance proof) thumbnail")
    ap.add_argument("--bg", required=True, type=Path, help="Killfeed background frame")
    ap.add_argument("--avatar", required=True, type=Path, help="POV HLTV cutout PNG")
    ap.add_argument("--name", required=True)
    ap.add_argument("--score", required=True, help="K-D, e.g. 23-14")
    ap.add_argument("--sub", default="", help="Line under the score (e.g. DUO w/ MAGIXX)")
    ap.add_argument("--costar", type=Path, default=None, help="Cutout to the right of POV (alias for --costar-right)")
    ap.add_argument("--costar-left", type=Path, default=None, help="Cutout to the left of POV")
    ap.add_argument("--costar-right", type=Path, default=None, help="Cutout to the right of POV")
    ap.add_argument("--badge", default=BADGE_DEFAULT)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    for p, label in ((args.bg, "bg"), (args.avatar, "avatar")):
        if not p.is_file():
            sys.exit(f"[ERR] {label} not found: {p}")
    if args.costar is not None and not args.costar.is_file():
        sys.exit(f"[ERR] costar not found: {args.costar}")
    if args.costar_left is not None and not args.costar_left.is_file():
        sys.exit(f"[ERR] costar-left not found: {args.costar_left}")
    if args.costar_right is not None and not args.costar_right.is_file():
        sys.exit(f"[ERR] costar-right not found: {args.costar_right}")
    out = render(
        bg=args.bg,
        main_avatar=args.avatar,
        name=args.name,
        score=args.score,
        dest=args.out,
        sub=args.sub,
        costar_avatar=args.costar,
        costar_left=args.costar_left,
        costar_right=args.costar_right,
        badge=args.badge,
    )
    print(f"[OK] {out}")


if __name__ == "__main__":
    main()
