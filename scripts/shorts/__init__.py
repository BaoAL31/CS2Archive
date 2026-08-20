from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

RENDERS_DIR = _PROJECT_ROOT / "renders"


def resolve_output_dir(demo_path: str | Path, player: str | None = None) -> Path:
    """Return the shorts base output directory for a demo.

    Both HLTV and FACEIT demos use ``renders/hl-{demo_stem}/`` (``hl-`` prefix).
    Per-short ``shorts-{slug}/`` subdirectories are created by the caller.

    The ``player`` argument is accepted for backwards compatibility but ignored.

    Creates the directory if it does not already exist.
    """
    demo = Path(demo_path).resolve()
    normalized = str(demo).replace("\\", "/")
    demo_stem = demo.stem

    if "demos/hltv" in normalized:
        output_dir = RENDERS_DIR / f"hl-{demo_stem}"
    elif "demos/faceit" in normalized:
        output_dir = RENDERS_DIR / f"hl-{demo_stem}"
    else:
        # Any other demo location (e.g. CS2UtilArchive's demos/extracted) —
        # use the demo stem so it still lands in renders/hl-{stem}/.
        output_dir = RENDERS_DIR / f"hl-{demo_stem}"

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
