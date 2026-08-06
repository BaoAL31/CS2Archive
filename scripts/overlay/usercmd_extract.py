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

# CS2 usercmd values (from the vendored parser's correct delta decode):
#  forwardmove>0=W, <0=S; leftmove>0=A (strafe left), <0=D (strafe right).
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
    return signals_from_rows(_parse_rows(_run_cli(demo_path, steam_id)))


def signals_from_rows(rows: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in rows:
        out[r["tick"]] = {
            "w": 1 if r["fw"] > 0.5 else 0,
            "s": 1 if r["fw"] < -0.5 else 0,
            "a": 1 if r["lf"] > 0.5 else 0,   # leftmove > 0 = strafe LEFT (A)
            "d": 1 if r["lf"] < -0.5 else 0,  # leftmove < 0 = strafe RIGHT (D)
            "duck": 1 if (r["b1"] & _BIT_DUCK) else 0,
            "walk": 1 if (r["b1"] & _BIT_WALK) else 0,
            "lmb": 1 if (r["b1"] & _BIT_M1) else 0,
            "rmb": 1 if (r["b1"] & _BIT_M2) else 0,
        }
    return out


def _run_cli(demo_path, steam_id: str) -> str:
    cli = _cli_path()
    r = subprocess.run(
        [str(cli), str(demo_path), str(steam_id)],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        raise RuntimeError(f"button_extract failed: {(r.stderr or '')[:500]}")
    return r.stdout


def _parse_rows(stdout: str) -> list[dict]:
    """Parse CLI output into rows: tick,b1,fw,lf,mdx,mdy,yaw,px,py."""
    rows = []
    for line in stdout.splitlines():
        p = line.split(",")
        if len(p) < 9:
            continue
        try:
            rows.append({
                "tick": int(p[0]), "b1": int(p[1]),
                "fw": float(p[2]), "lf": float(p[3]),
                "mdx": int(p[4]), "mdy": int(p[5]),
                "yaw": float(p[6]), "px": float(p[7]), "py": float(p[8]),
            })
        except ValueError:
            continue
    return rows


def validate_keys_vs_velocity(demo_path, steam_id: str) -> dict:
    """Sanity-check extracted A/D keys against the player's actual velocity.

    Runs the CLI, then checks for ticks where donk is clearly strafing (one
    strafe key, moving fast) whether the pressed direction agrees with the
    actual movement relative to his view yaw. High mismatch => key->direction
    mapping wrong (e.g. A/D swap) or data misaligned.
    """
    return _validate_from_rows(_parse_rows(_run_cli(demo_path, steam_id)))


def _validate_from_rows(rows: list[dict]) -> dict:
    """Validate A/D key extraction against velocity from pre-parsed CLI rows."""
    import math

    total = a_mismatch = d_mismatch = 0
    a_ok = d_ok = 0
    for i in range(4, len(rows)):
        cur = rows[i]; prev = rows[i - 4]
        vx = cur["px"] - prev["px"]; vy = cur["py"] - prev["py"]
        if vx * vx + vy * vy < 16:
            continue  # not moving meaningfully
        yaw_rad = math.radians(cur["yaw"])
        lx, ly = -math.sin(yaw_rad), math.cos(yaw_rad)  # LEFT vector
        perp = vx * lx + vy * ly  # >0 = moving LEFT, <0 = moving RIGHT
        a = 1 if cur["lf"] > 0.5 else 0
        d = 1 if cur["lf"] < -0.5 else 0
        if a and not d:
            total += 1
            if perp > 0: a_ok += 1
            else: a_mismatch += 1
        elif d and not a:
            total += 1
            if perp < 0: d_ok += 1
            else: d_mismatch += 1
    n = a_ok + a_mismatch + d_ok + d_mismatch
    return {
        "strafe_samples": n,
        "a_ok": a_ok, "a_mismatch": a_mismatch,
        "d_ok": d_ok, "d_mismatch": d_mismatch,
        "a_mismatch_rate": (a_mismatch / (a_ok + a_mismatch)) if (a_ok + a_mismatch) else None,
        "d_mismatch_rate": (d_mismatch / (d_ok + d_mismatch)) if (d_ok + d_mismatch) else None,
    }
