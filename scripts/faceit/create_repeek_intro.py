"""FACEIT intro built from the existing Repeek left/right stat panes.

The intro frame is composed from the two panes the Repeek capture already
produces — ``repeek_left.png`` / ``repeek_right.png`` (the real "Last N matches"
stats UI, captured by ``scrapers.repeek_snapshot``) — as **rounded pop-up cards**
inset from the frame edges with anti-aliased corners. Nothing is redrawn by us,
the panes never reach the corners, and the middle is left transparent so the
buy-phase footage shows through.

Produces the artefact the pipeline needs:
    <output>/intro.png            2560x1440 RGBA, ready for ``intro_prepend.py``
    <output>/intro_details.json   provenance (match id, panels used)

Capture is resumable: existing ``renders/stat-strips/<match_id>/repeek_left.png``
+ ``repeek_right.png`` are reused unless ``--force``. Capture requires the Repeek
extension in ``.sessions/faceit`` and a logged-in FACEIT session (headed Chrome),
same as the demo-download flow.

Usage:
    python scripts/faceit/create_repeek_intro.py --match-id <id>
    python scripts/faceit/create_repeek_intro.py --backlog backlog/faceit/high/<slug>.json
    python scripts/faceit/create_repeek_intro.py --match-id <id> --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402

ensure()

from imgutil import drop_shadow, rounded_layer  # noqa: E402

from scrapers.repeek_snapshot import (  # noqa: E402
    RepeekCaptureError,
    assert_column_pngs,
    run_standalone,
    strips_dir,
)

OUT_W, OUT_H = 2560, 1440
PANE_MARGIN = 56
PANE_RADIUS = 28
SHADOW_OFFSET = (8, 8)
SHADOW_BLUR = 14
SHADOW_SPREAD = 0
SHADOW_OPACITY = 0.28


def match_id_from_backlog(backlog: Path) -> str:
    data = json.loads(Path(backlog).read_text(encoding="utf-8"))
    mid = str(data.get("faceit_match_id") or "").strip()
    if not mid:
        raise SystemExit(f"[ERROR] no faceit_match_id in {backlog}")
    return mid


def _pane_layer(img: Image.Image, height: int) -> Image.Image:
    """Scale pane to ``height`` and round its corners (anti-aliased)."""
    img = img.convert("RGBA")
    width = max(1, int(round(img.width * height / img.height)))
    img = img.resize((width, height), Image.LANCZOS)
    return rounded_layer(img, PANE_RADIUS)


def _popup(frame: Image.Image, pane: Image.Image, x: int, y: int) -> None:
    shadow, pad = drop_shadow(
        pane,
        offset=SHADOW_OFFSET,
        blur=SHADOW_BLUR,
        spread=SHADOW_SPREAD,
        opacity=SHADOW_OPACITY,
    )
    frame.alpha_composite(shadow, (x - pad, y - pad))
    frame.alpha_composite(pane, (x, y))


def _pane_layout(
    left: Image.Image,
    right: Image.Image,
    w: int,
    h: int,
) -> tuple[Image.Image, Image.Image, tuple[int, int], tuple[int, int]]:
    inner_h = max(1, h - 2 * PANE_MARGIN)
    l = _pane_layer(left, inner_h)
    r = _pane_layer(right, inner_h)
    avail = w - 2 * PANE_MARGIN
    if l.width + r.width > avail:
        scale = avail / (l.width + r.width)
        nh = max(1, int(inner_h * scale))
        l = _pane_layer(left, nh)
        r = _pane_layer(right, nh)
    return l, r, (PANE_MARGIN, (h - l.height) // 2), (
        w - PANE_MARGIN - r.width, (h - r.height) // 2,
    )


def compose_panes(left: Image.Image, right: Image.Image,
                  w: int = OUT_W, h: int = OUT_H) -> Image.Image:
    frame = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    l, r, left_pos, right_pos = _pane_layout(left, right, w, h)
    _popup(frame, l, *left_pos)
    _popup(frame, r, *right_pos)
    return frame


def pane_layers(left: Image.Image, right: Image.Image,
                w: int = OUT_W, h: int = OUT_H
                ) -> tuple[Image.Image, Image.Image, list[int], list[int]]:
    l, r, left_pos, right_pos = _pane_layout(left, right, w, h)
    left_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    right_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    _popup(left_layer, l, *left_pos)
    _popup(right_layer, r, *right_pos)
    left_box = left_layer.getchannel("A").getbbox() or (0, 0, 0, 0)
    right_box = right_layer.getchannel("A").getbbox() or (0, 0, 0, 0)
    left_rect = [left_box[0], left_box[1], left_box[2] - left_box[0], left_box[3] - left_box[1]]
    right_rect = [right_box[0], right_box[1], right_box[2] - right_box[0], right_box[3] - right_box[1]]
    return left_layer, right_layer, left_rect, right_rect


def ensure_capture(match_id: str, *, force: bool) -> Path:
    d = strips_dir(match_id)
    left, right = d / "repeek_left.png", d / "repeek_right.png"
    if left.is_file() and right.is_file() and not force:
        assert_column_pngs(left, right)
        print(f"[intro] reusing {left.name} + {right.name}", flush=True)
        return d
    print(f"[intro] capturing Repeek left/right panes for {match_id} ...", flush=True)
    out = run_standalone(match_id)
    left, right = out / "repeek_left.png", out / "repeek_right.png"
    if not left.is_file() or not right.is_file():
        raise RepeekCaptureError(
            "REPEEK_SCREENSHOT", f"capture did not produce panes in {out}",
        )
    return out


def build_intro(match_id: str, output_dir: Path, *, force: bool) -> Path:
    d = ensure_capture(match_id, force=force)
    left_png, right_png = d / "repeek_left.png", d / "repeek_right.png"
    left = Image.open(left_png)
    right = Image.open(right_png)
    frame = compose_panes(left, right, OUT_W, OUT_H)
    left_layer, right_layer, left_rect, right_rect = pane_layers(left, right, OUT_W, OUT_H)
    output_dir.mkdir(parents=True, exist_ok=True)
    intro_path = output_dir / "intro.png"
    left_path = output_dir / "intro_left.png"
    right_path = output_dir / "intro_right.png"
    for stale in (
        "intro_displacement_x.png",
        "intro_displacement_y.png",
        "intro_refraction_mask.png",
        "intro_caustic_mask.png",
    ):
        (output_dir / stale).unlink(missing_ok=True)
    frame.save(intro_path, "PNG")
    left_layer.save(left_path, "PNG")
    right_layer.save(right_path, "PNG")
    (output_dir / "intro_details.json").write_text(json.dumps({
        "match_id": match_id,
        "panes": [str(left_png), str(right_png)],
        "pane_sizes": [[left.width, left.height], [right.width, right.height]],
        "intro_size": [OUT_W, OUT_H],
        "builder": "create_repeek_intro",
        "effect": "pane_drop_shadow",
        "effect_version": 5,
        "layers": [left_path.name, right_path.name],
        "left_rect": left_rect,
        "right_rect": right_rect,
        "shadow_offset": list(SHADOW_OFFSET),
        "shadow_blur": SHADOW_BLUR,
        "shadow_spread": SHADOW_SPREAD,
        "shadow_opacity": SHADOW_OPACITY,
    }, indent=2), encoding="utf-8")
    print(f"[intro] {intro_path} ({OUT_W}x{OUT_H}) from left/right panes", flush=True)
    return intro_path


def main() -> None:
    ap = argparse.ArgumentParser(description="FACEIT intro from Repeek left/right panes")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--match-id", help="FACEIT match id (1-xxxxxxxx-... )")
    src.add_argument("--backlog", type=Path, help="backlog card with faceit_match_id")
    ap.add_argument("--output", type=Path, default=None,
                    help="output dir (default renders/intro-<match_id>)")
    ap.add_argument("--force", action="store_true",
                    help="re-capture even if left/right panes exist")
    args = ap.parse_args()

    match_id = str(args.match_id) if args.match_id else match_id_from_backlog(args.backlog)
    output_dir = args.output or PROJECT_ROOT / "renders" / f"intro-{match_id}"
    try:
        build_intro(match_id, output_dir, force=args.force)
    except RepeekCaptureError as e:
        print(json.dumps(e.payload), flush=True)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
