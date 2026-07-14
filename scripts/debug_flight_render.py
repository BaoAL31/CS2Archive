#!/usr/bin/env python3
"""Debug: render ONE utility flight clip with proper camera injection.

Proves the fix for overlay_pov.py Bug A (random POV instead of grenade chase).
Uses CS2UtilArchive's run_csdm() with flight_* kwargs so the inject poll thread
writes spec_goto chase-cam commands into csdm actions JSON while CS2 runs.

Run:
  python scripts/debug_flight_render.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

# Point at CS2UtilArchive (mirror overlay_pov.py path setup)
_CS2UTIL_ROOT = Path(r"D:\Projects\CS2UtilArchive")
for _p in (str(_CS2UTIL_ROOT / "scripts"), str(_CS2UTIL_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.render.csdm import build_flight_command, run_csdm
from scripts.render.paths import actions_path_for_demo, flight_clip_name

# -- Inputs --------------------------------------------------------------
DEMO = Path(r"D:\Projects\CS2Archive\demos\hltv\furia-vs-falcons-iem-cologne-major\furia-vs-falcons-m3-inferno.dem")
DATA_DIR = Path(r"D:\Projects\CS2UtilArchive\results\iem_cologne_major_2026\data\demo=2395002-furia-vs-falcons-m3-inferno")
THROW_ID = "furia-vs-falcons-m3-inferno:e953:s1"
RENDER_DIR = Path(r"D:\Projects\CS2Archive\renders\flight_debug\e953")


def main() -> int:
    print(f"[debug] demo      = {DEMO}")
    print(f"[debug] data_dir  = {DATA_DIR}")
    print(f"[debug] throw_id  = {THROW_ID}")
    print(f"[debug] render_dir= {RENDER_DIR}")
    print()

    # Load throw row
    throws = pd.read_parquet(DATA_DIR / "throws.parquet")
    row = throws[throws["throw_id"] == THROW_ID].iloc[0]
    throw_tick = int(row["throw_tick"])
    det = row.get("detonate_tick")
    import math
    if det is None or (isinstance(det, float) and math.isnan(det)):
        detonate_tick = int(row["land_tick"])
        print(f"[debug] detonate_tick NaN -> using land_tick={detonate_tick}")
    else:
        detonate_tick = int(det)
    land_tick = int(row["land_tick"])
    flight_ticks = int(row["flight_ticks"])
    throw_pose = {
        "x": float(row["throw_x"]),
        "y": float(row["throw_y"]),
        "z": float(row["throw_z"]),
        "pitch": float(row["throw_pitch"]),
        "yaw": float(row["throw_yaw"]),
        "rx": float(row["throw_x"]),
        "ry": float(row["throw_y"]),
        "rz": float(row["throw_z"]),
    }
    print(f"[debug] throw_tick    = {throw_tick}")
    print(f"[debug] detonate_tick = {detonate_tick}  land_tick={land_tick}  flight_ticks={flight_ticks}")
    print(f"[debug] throw_pose    = x={throw_pose['x']:.2f} y={throw_pose['y']:.2f} z={throw_pose['z']:.2f}"
          f" pitch={throw_pose['pitch']:.2f} yaw={throw_pose['yaw']:.2f}")

    # Load trajectory filtered by throw_id
    traj = pd.read_parquet(DATA_DIR / "trajectories.parquet")
    traj_df = traj[traj["throw_id"] == THROW_ID].copy().sort_values("tick")
    print(f"[debug] trajectory rows = {len(traj_df)}  tick {int(traj_df.tick.min())}->{int(traj_df.tick.max())}")
    if traj_df.empty:
        print("[debug] NO TRAJECTORY — injection will fail")
        return 2
    print()

    # Build job + command
    clip_name = flight_clip_name(THROW_ID)  # -> flight_furia-vs-falcons-m3-inferno_e953_s1
    print(f"[debug] clip_name = {clip_name}  (NOT throw_flight_ — fix Bug B)")
    job = {
        "demo_path": str(DEMO.resolve()),
        "throw_tick": throw_tick,
        "detonate_tick": detonate_tick,
        "throw_id": THROW_ID,
        "output_name": clip_name,
    }
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    cmd = build_flight_command(job, str(RENDER_DIR))
    print(f"[debug] csdm cmd: {' '.join(cmd[:6])} ... --output-file-name {clip_name}.mp4")
    print()

    # Run with camera injection (the fix). run_csdm spawns inject poll thread
    # that prints "[inject] flight (N ticks)" when spec_goto commands written.
    t0 = time.time()
    out = run_csdm(
        cmd,
        "flight-debug",
        demo_path=str(DEMO.resolve()),
        flight_throw_tick=throw_tick,
        flight_detonate_tick=detonate_tick,
        flight_trajectories_df=traj_df,
        flight_throw_pose=throw_pose,
        flight_smooth=0.75,
        orbit_handoff_delay_ticks=0,
    )
    print()
    print(f"[debug] run_csdm returned: {out}")
    print(f"[debug] elapsed {time.time()-t0:.0f}s")

    # Inspect actions JSON to confirm spec_goto injection
    actions_path = actions_path_for_demo(str(DEMO.resolve()))
    print(f"[debug] actions file: {actions_path}  exists={actions_path.is_file()}")
    if actions_path.is_file():
        import json
        try:
            raw = json.loads(actions_path.read_text(encoding="utf-8"))
            n_spec_goto = 0
            tick_min, tick_max = None, None
            for seq in raw if isinstance(raw, list) else []:
                for a in seq.get("actions", []):
                    if "spec_goto" in a.get("cmd", ""):
                        n_spec_goto += 1
                        t = a.get("tick")
                        if t is not None:
                            tick_min = t if tick_min is None else min(tick_min, t)
                            tick_max = t if tick_max is None else max(tick_max, t)
            print(f"[debug] spec_goto commands in actions: {n_spec_goto}  tick range {tick_min}->{tick_max}")
            if n_spec_goto:
                print("[debug] >>> INJECTION WORKED — chase cam written <<<")
            else:
                print("[debug] >>> NO INJECTION — still random POV bug <<<")
        except Exception as e:
            print(f"[debug] actions parse error: {e}")

    # Inspect output
    if out and out.is_file():
        mb = out.stat().st_size / 1e6
        print(f"[debug] OUTPUT mp4: {out}  ({mb:.1f} MB)")
        print("[debug] Open it. Grenade should be center-frame, camera chasing flight.")
        return 0
    print("[debug] NO OUTPUT mp4 produced")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
