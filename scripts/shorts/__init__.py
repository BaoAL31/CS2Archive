from __future__ import annotations

import shutil
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

RENDERS_DIR = _PROJECT_ROOT / "renders"


def resolve_output_dir(demo_path: str | Path, player: str | None = None) -> Path:
    """Return the shorts base output directory for a demo.

    Both HLTV and FACEIT demos use ``renders/shorts/shorts-{demo_stem}/``.
    Per-short ``shorts-{slug}/`` subdirectories are created by the caller.

    The ``player`` argument is accepted for backwards compatibility but ignored.

    Does not create the directory — callers mkdir when they actually write a short.
    """
    demo = Path(demo_path).resolve()
    normalized = str(demo).replace("\\", "/")
    demo_stem = demo.stem

    if "demos/hltv" not in normalized and "demos/faceit" not in normalized:
        raise ValueError(f"unknown demo path: {demo}")
    return RENDERS_DIR / "shorts" / f"shorts-{demo_stem}"


def discard_empty_shorts_dir(base: Path) -> None:
    """Remove ``renders/shorts/shorts-{stem}/`` when it has no per-short folders."""
    if not base.is_dir():
        return
    if base.parent.name != "shorts" or not base.name.startswith("shorts-"):
        return
    try:
        has_short = any(
            p.is_dir() and p.name.startswith("shorts-") for p in base.iterdir()
        )
    except FileNotFoundError:
        return
    if has_short:
        return
    shutil.rmtree(base, ignore_errors=True)
