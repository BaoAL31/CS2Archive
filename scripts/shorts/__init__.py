from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

RENDERS_DIR = _PROJECT_ROOT / "renders"


def resolve_output_dir(demo_path: str | Path, player: str | None = None) -> Path:
    """Return the shorts output directory for a demo (per ADR 0004 + pollution guard).

    Shorts are folded into a single per-match folder, not per-player — otherwise
    a 10-player match would create 10 near-empty ``renders/`` siblings. The
    player's identity travels inside each rendered short (POV steam_id), not in
    the directory layout.

    For HLTV demos (under ``demos/hltv/``) returns
    ``renders/pov-{demo_stem}/shorts/`` (sibling to all per-player POV dirs).

    For FACEIT demos (under ``demos/faceit/``) returns
    ``renders/hl-{demo_stem}/shorts/``.

    The ``player`` argument is accepted for backwards compatibility but ignored.

    Creates the directory if it does not already exist.
    """
    demo = Path(demo_path).resolve()
    normalized = str(demo).replace("\\", "/")
    demo_stem = demo.stem

    if "demos/hltv" in normalized:
        output_dir = RENDERS_DIR / f"pov-{demo_stem}" / "shorts"
    elif "demos/faceit" in normalized:
        output_dir = RENDERS_DIR / f"hl-{demo_stem}" / "shorts"
    else:
        raise ValueError(f"resolve_output_dir: unknown demo path: {demo}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
