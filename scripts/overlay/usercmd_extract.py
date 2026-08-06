"""Load correct CS2 usercmd input for the input overlay.

demoparser2 (0.41.x) returns misaligned usercmd buttonstate on recent FACEIT demos
(delta_data decoding is broken upstream — PR #343 not merged). This module instead
runs the vendored-parser Rust CLI (tools/button_extract, built from unicbm/demotracer's
patched demoparser which decodes delta_data correctly) to get per-tick, correctly-aligned
movement (forwardmove/leftmove) and buttonstate, and converts them to overlay signals.

Result: { tick: {"w","a","s","d","duck","walk","lmb","rmb"} } (jump comes from
is_airborne/velocity_Z in the existing demoparser2 path).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CLI = _PROJECT_ROOT / "tools" / "button_extract" / "target" / "release" / "button_extract.exe"

# CS2 usercmd buttonstate bits (confirmed by correlating against donk's movement):
#  W=bit3(8), S=bit4(16), A=bit10(1024), D=bit9(512) [A/D swapped vs standard IN_*],
#  duck=bit2(4), walk=bit16(65536), M1=bit0(1), M2=bit11(2048).
_BIT_DUCK = 4
_BIT_WALK = 1 << 16
_BIT_M1 = 1
_BIT_M2 = 1 << 11


def _cli_path() -> Path:
    if not _CLI.is_file():
        raise RuntimeError(
            f"button_extract binary not built: {_CLI}\n"
            "Build it once with: cargo build --release --manifest-path "
            f"{_PROJECT_ROOT / 'tools' / 'button_extract' / 'Cargo.toml'}"
        )
    return _CLI


def corrected_signals(demo_path, steam_id: str) -> dict[int, dict]:
    """Run the Rust CLI and return per-tick overlay signals for the player."""
    cli = _cli_path()
    r = subprocess.run(
        [str(cli), str(demo_path), str(steam_id)],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"button_extract failed: {(r.stderr or '')[:500]}")
    out: dict[int, dict] = {}
    for line in r.stdout.splitlines():
        p = line.split(",")
        if len(p) < 6:
            continue
        try:
            tick = int(p[0]); b1 = int(p[1]); fw = float(p[2]); lf = float(p[3])
        except ValueError:
            continue
        out[tick] = {
            "w": 1 if fw > 0.5 else 0,
            "s": 1 if fw < -0.5 else 0,
            "a": 1 if lf < -0.5 else 0,
            "d": 1 if lf > 0.5 else 0,
            "duck": 1 if (b1 & _BIT_DUCK) else 0,
            "walk": 1 if (b1 & _BIT_WALK) else 0,
            "lmb": 1 if (b1 & _BIT_M1) else 0,
            "rmb": 1 if (b1 & _BIT_M2) else 0,
        }
    return out
