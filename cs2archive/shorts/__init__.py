from __future__ import annotations

import shutil
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

RENDERS_DIR = _PROJECT_ROOT / "renders"


def resolve_output_dir(demo_path: str | Path, player: str | None = None) -> Path:
    """Return the shorts base output directory for a demo.

    New home (per-POV): ``renders/pov-{stem}_{nick}/shorts/`` — callers pass
    the POV folder (or nick) they resolved from backlog cards. Without one,
    falls back to the legacy ``renders/shorts/shorts-{demo_stem}/`` tree so
    a short is never dropped for want of a folder.

    The ``player`` argument is accepted for backwards compatibility but ignored;
    pass an explicit POV dir instead.

    Does not create the directory — callers mkdir when they actually write a short.
    """
    demo = Path(demo_path).resolve()
    normalized = str(demo).replace("\\", "/")
    demo_stem = demo.stem

    if "demos/hltv" not in normalized and "demos/faceit" not in normalized:
        raise ValueError(f"unknown demo path: {demo}")
    return RENDERS_DIR / "shorts" / f"shorts-{demo_stem}"


def discard_empty_shorts_dir(base: Path) -> None:
    """Remove an empty shorts base dir (never the POV folder itself).

    Handles the per-POV ``{pov}/shorts/`` layout and the legacy
    ``renders/shorts/shorts-{stem}/`` tree.
    """
    if not base.is_dir():
        return
    if base.name == "shorts" and base.parent.name.startswith("pov-"):
        pass
    elif base.name.startswith("shorts-") and base.parent.name == "shorts":
        pass
    else:
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
