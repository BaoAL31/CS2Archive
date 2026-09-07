"""Uniformize all cached avatars to a standard chest-up framing.

Canonical logic lives in scrapers.player_images.normalize_avatar_image;
this script just walks demos/avatars/ and applies it. New fetches are
auto-normalized at save time (see _save_avatar_bytes_sync), so this is only
needed for backfill or after refresh_avatars.py (which also saves normalized).

Usage:
    python scripts/pov/normalize_avatars.py [--dry-run] [--nick <name>]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure as _ensure_path

_ensure_path()

from scrapers.player_images import (
    AVATAR_CANVAS_H,
    AVATAR_CANVAS_W,
    AVATAR_HEAD_TOP,
    AVATAR_HEAD_W,
    _avatar_head_width,
    _trim_avatar,
    normalize_avatar_image,
)

AVATAR_DIR = PROJECT_ROOT / "demos" / "avatars"


def _already_standard(im: Image.Image) -> float | None:
    """Return head width when the file already meets the standard, else None."""
    if im.size != (AVATAR_CANVAS_W, AVATAR_CANVAS_H):
        return None
    top = im.getchannel("A").getbbox()
    if not top or abs(top[1] - AVATAR_HEAD_TOP) > 4:
        return None
    hw = _avatar_head_width(_trim_avatar(im))
    if hw and abs(hw - AVATAR_HEAD_W) <= 6:
        return hw
    return None


def normalize_one(path: Path, dry_run: bool = False) -> str:
    try:
        im = Image.open(path).convert("RGBA")
    except Exception as e:
        return f"SKIP unreadable ({e})"
    hw = _already_standard(im)
    if hw:
        return f"SKIP already standard (head_w={hw:.0f})"
    out = normalize_avatar_image(im)
    if out is None:
        return "SKIP unusable (opaque/empty/no-head)"
    if not dry_run:
        out.save(path, "PNG")
    return f"OK {im.size}->{out.size}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--nick", default=None, help="Only process one nickname")
    args = ap.parse_args()

    files = sorted(AVATAR_DIR.glob("*/hltv/*")) + sorted(AVATAR_DIR.glob("*/faceit/*"))
    files = [f for f in files if f.is_file() and f.suffix.lower() in (".png", ".webp")]
    if args.nick:
        files = [f for f in files if f.parent.parent.name == args.nick.strip().lower()]

    ok = skip = 0
    for f in files:
        rel = f.relative_to(AVATAR_DIR)
        res = normalize_one(f, dry_run=args.dry_run)
        print(f"  [{rel}] {res}")
        if res.startswith("OK"):
            ok += 1
        else:
            skip += 1
    print(f"\nDone: {ok} normalized, {skip} skipped")


if __name__ == "__main__":
    main()
