#!/usr/bin/env python3
"""Validate keyboard overlay extraction against ground truth.

Two checks:
  1. Per-tick match vs CS2UtilArchive input_overlay.parquet (known-good extraction)
  2. Velocity sanity: when FORWARD/BACK/LEFT/RIGHT pressed and player is moving,
     velocity direction must align with view-yaw projected onto those keys.

Run:
    python tests/test_overlay_extraction.py <demo> <steam_id> [--round N]
"""
from __future__ import annotations
import argparse, math, sys
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

# Point at CS2UtilArchive
_CS2UTIL = Path(r"D:\Projects\CS2UtilArchive")
for _p in (str(_CS2UTIL / "scripts"), str(_CS2UTIL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scripts.input_overlay_decode import overlay_tick_from_row
from scripts.render.overlay_layout import _OVERLAY_SIGNALS


# --- Helpers -------------------------------------------------------

def _find_demo_data_dir(demo: Path) -> Path | None:
    results = _CS2UTIL / "results"
    if not results.is_dir():
        return None
    slug = demo.stem
    for proj in results.iterdir():
        dd = proj / "data"
        if not dd.is_dir():
            continue
        for d in dd.iterdir():
            if d.is_dir() and d.name.startswith("demo=") and slug in d.name:
                return d
    return None


def load_ground_truth(demo: Path, steam_id: int) -> pd.DataFrame:
    """Load per-tick states for THIS player from CS2UtilArchive input_overlay.parquet (known-good)."""
    data_dir = _find_demo_data_dir(demo)
    if data_dir is None:
        sys.exit("[ERR] No CS2UtilArchive data dir for this demo")
    p = data_dir / "input_overlay.parquet"
    if not p.is_file():
        sys.exit(f"[ERR] {p} missing")
    df = pd.read_parquet(p)

    # parquet has no thrower_steamid column. Filter via throws.parquet throw_ids.
    tp = data_dir / "throws.parquet"
    if tp.is_file():
        tdf = pd.read_parquet(tp)
        niko_throws = tdf[tdf["thrower_steamid"] == int(steam_id)]["throw_id"].astype(str).tolist()
        niko_set = set(niko_throws)
        before = len(df)
        df = df[df["throw_id"].astype(str).isin(niko_set)].copy()
        print(f"  Filtered {before} -> {len(df)} rows for steamid {steam_id} via {len(niko_set)} throw_ids")
    return df


def extract_per_tick(demo: Path, steam_id: int, fields, tick_lo: int, tick_hi: int) -> pd.DataFrame:
    """Parse ticks via demoparser2 -> per-tick states dict (e.g. what overlay_pov does)."""
    p = DemoParser(str(demo))
    df = p.parse_ticks(list(fields), players=[int(steam_id)])
    df = df[(df["tick"] >= tick_lo) & (df["tick"] <= tick_hi)].copy()
    return df.sort_values("tick")


def row_to_states(row: pd.Series) -> dict[str, int]:
    states, _ = overlay_tick_from_row(row, apply_jump_inference=False)
    return states


# --- Tests ---------------------------------------------------------

def test_vs_ground_truth(demo: Path, steam_id: int) -> int:
    """For every tick in input_overlay.parquet, our overlay_tick_from_row MUST match."""
    gt = load_ground_truth(demo, steam_id)
    if gt.empty:
        print("SKIP: no ground truth rows")
        return 0
    tick_lo, tick_hi = int(gt["tick"].min()), int(gt["tick"].max())
    print(f"Ground truth: {len(gt)} rows, ticks {tick_lo}..{tick_hi}")

    # Re-parse same tick window with full DEMOPARSER_TICK_FIELDS
    from scripts.input_overlay_decode import DEMOPARSER_TICK_FIELDS
    raw = extract_per_tick(demo, steam_id, DEMOPARSER_TICK_FIELDS, tick_lo, tick_hi)
    our_states_by_tick = {}
    for _, row in raw.iterrows():
        # Include yaw/velocity for second test
        our_states_by_tick[int(row["tick"])] = row

    mismatches = 0
    checked = 0
    per_sig_mismatch = {s: 0 for s in _OVERLAY_SIGNALS}
    for _, g in gt.iterrows():
        tick = int(g["tick"])
        row = our_states_by_tick.get(tick)
        if row is None:
            continue
        checked += 1
        states = row_to_states(row)
        for sig in _OVERLAY_SIGNALS:
            gt_v = int(g[f"{sig}_state"])
            if states[sig] != gt_v:
                mismatches += 1
                per_sig_mismatch[sig] += 1
    print(f"  Checked {checked} ticks")
    if checked == 0:
        print("FAIL: no tick overlap")
        return 1
    rate = (mismatches / (checked * len(_OVERLAY_SIGNALS))) * 100
    print(f"  Total mismatches: {mismatches} ({rate:.2f}%)")
    for s, n in per_sig_mismatch.items():
        if n:
            print(f"    {s}: {n} mismatches")
    if rate > 0.5:
        print("FAIL: extraction diverges from ground truth > 0.5%")
        return 1
    print("PASS: extraction matches ground truth")
    return 0


def test_velocity_yaw(demo: Path, steam_id: int, round_num: int | None = None) -> int:
    """Per-tick: when movement keys pressed AND player moving, vel must align with yaw-direction."""
    p = DemoParser(str(demo))
    fields = ["tick", "steamid", "FORWARD", "LEFT", "RIGHT", "BACK",
              "ducked", "ducking", "in_duck_jump", "old_jump_pressed", "buttons",
              "yaw", "X", "Y", "Z", "velocity_X", "velocity_Y", "velocity_Z"]
    df = p.parse_ticks(fields, players=[int(steam_id)])

    if round_num is not None:
        # Load round tick range (start_tick = prev round end_tick+1)
        dd = _find_demo_data_dir(demo)
        rd = dd / "rounds.parquet" if dd else None
        if rd and rd.is_file():
            rdf = pd.read_parquet(rd).sort_values("round_num")
            prev_end = 0
            lo = hi = 0
            for _, rr in rdf.iterrows():
                rn = int(rr["round_num"])
                start_t = prev_end + 1
                end_t = int(rr["end_tick"])
                if rn == int(round_num):
                    lo, hi = start_t, end_t
                    break
                prev_end = end_t
            if lo and hi:
                df = df[(df["tick"] >= lo) & (df["tick"] <= hi)].copy()
                print(f"Velocity test: round {round_num} (ticks {lo}..{hi}), {len(df)} rows")
    else:
        print(f"Velocity test: full demo, {len(df)} rows")

    SPEED_MIN = 60.0  # below this we may be frozen / not actually moving
    mismatch = 0
    checked = 0
    for _, row in df.iterrows():
        vx = row.get("velocity_X"); vy = row.get("velocity_Y")
        if pd.isna(vx) or pd.isna(vy):
            continue
        speed = math.hypot(vx, vy)
        f = bool(row.get("FORWARD")); b = bool(row.get("BACK"))
        l = bool(row.get("LEFT")); r = bool(row.get("RIGHT"))
        keys = (f or b or l or r)
        if not keys or speed < SPEED_MIN:
            continue
        # Build commanded direction from yaw (deg)
        yaw = float(row.get("yaw", 0.0))
        yaw_rad = math.radians(yaw)
        # CS2: forward = (cos yaw, sin yaw). Confirm with sample fit; flip if needed.
        fwd_x, fwd_y = math.cos(yaw_rad), math.sin(yaw_rad)
        # Left vector (perpendicular, yaw+90 deg = turn-left in CS right-hand)
        left_x, left_y = -math.sin(yaw_rad), math.cos(yaw_rad)
        cmd_x = cmd_y = 0.0
        if f:
            cmd_x += fwd_x; cmd_y += fwd_y
        if b:
            cmd_x -= fwd_x; cmd_y -= fwd_y
        if l:
            cmd_x += left_x; cmd_y += left_y
        if r:
            cmd_x -= left_x; cmd_y -= left_y
        cmag = math.hypot(cmd_x, cmd_y)
        if cmag < 1e-6:
            continue
        cmd_x, cmd_y = cmd_x / cmag, cmd_y / cmag
        vel_x, vel_y = vx / speed, vy / speed
        dot = cmd_x * vel_x + cmd_y * vel_y
        # When walking forward-only, dot should be > 0 (>=0.5 typically).
        checked += 1
        if dot < 0.3:
            mismatch += 1
            if mismatch <= 10:
                print(f"  t{int(row['tick'])} F={f} B={b} L={l} R={r} yaw={yaw:.0f} "
                      f"v=({vx:.0f},{vy:.0f}) speed={speed:.0f} dot={dot:.2f}")
    print(f"  Checked {checked} moving-key ticks, {mismatch} vel-direction mismatches")
    if checked < 10:
        print("SKIP: insufficient moving samples")
        return 0
    rate = mismatch / checked
    if rate > 0.25:
        print(f"FAIL: {rate:.0%} of moving ticks have vel pointing wrong way (yaw convention?)")
        return 1
    print(f"PASS: velocity matches yaw-direction for {(1-rate):.0%} of moving ticks")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo", type=Path)
    ap.add_argument("steam_id")
    ap.add_argument("--round", type=int, default=None)
    args = ap.parse_args()
    sid = int(args.steam_id)
    rc1 = test_vs_ground_truth(args.demo, sid)
    rc2 = test_velocity_yaw(args.demo, sid, args.round)
    sys.exit(max(rc1, rc2))


if __name__ == "__main__":
    main()