from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure
ensure()

RENDERS_DIR = _PROJECT_ROOT / "renders"


def resolve_output_dir(demo_path: str | Path, player: str | None = None) -> Path:
    """Return the shorts output directory for a demo.

    For HLTV demos (under ``demos/hltv/``) returns
    ``renders/pov-{demo_stem}_{player}/shorts/``.

    For FACEIT demos (under ``demos/faceit/``) returns
    ``renders/hl-{demo_stem}/shorts/``.

    Creates the ``shorts/`` subfolder if it does not already exist.
    """
    demo = Path(demo_path).resolve()
    normalized = str(demo).replace("\\", "/")
    demo_stem = demo.stem

    if "demos/hltv" in normalized:
        if not player:
            raise ValueError(
                f"resolve_output_dir: player required for HLTV demo {demo}"
            )
        output_dir = RENDERS_DIR / f"pov-{demo_stem}_{player}" / "shorts"
    elif "demos/faceit" in normalized:
        output_dir = RENDERS_DIR / f"hl-{demo_stem}" / "shorts"
    else:
        raise ValueError(f"resolve_output_dir: unknown demo path: {demo}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir
