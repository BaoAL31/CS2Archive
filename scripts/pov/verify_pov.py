"""Post-render POV verification (tier 1, no OCR, no ML).

Catches the kensizor class: freecam stuck (frozen/drifting, no viewmodel) and
third-person segments without a visible gun. Runs on round clips BEFORE concat
so bad rounds can be re-rendered instead of shipped.

Method (per round clip, POV-ALIVE window only — death tick from analysis):
  - sample one frame every SAMPLE_EVERY_S seconds from t=1s to alive end
  - skip flashed / smoked / scoped frames (brightness gates)
  - weapon score: Sobel edge density in the viewmodel corner ROI
    (relative coords, resolution independent)
  - motion score: mean abs diff vs previous kept sample (160x90 gray)
Round BAD when frozen-share high (stuck cam) or weapon pass-rate low.

Known blind spots (tier 2, needs nameplate OCR):
  - director cut to a teammate clutch (first-person, gun out, wrong player)
  - CS2 third-person chase renders the spectated viewmodel, so
    third-person-with-visible-gun can pass the weapon gate
    (e.g. upper-tunnel follow). Motion gate still fires when stuck.
Tuned 2026-09-11 on kensizor (bad) vs control/recam renders (good).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _pathsetup import ensure  # noqa: E402
ensure()  # noqa: E402
from config import settings  # noqa: E402

SAMPLE_EVERY_S = 4.0
WEAPON_EDGE_PASS = 4.5
FROZEN_DIFF = 5.0
FLASH_BRIGHT = 200.0
SCOPE_DARK = 60.0
SMOKE_SAT = 0.05
WEAPON_PASS_RATE_MIN = 0.4
FROZEN_SHARE_MAX = 0.5
MIN_SAMPLES = 5
DEATH_GRACE_S = 2.0


@dataclass
class RoundVerdict:
    ok: bool
    reason: str
    weapon_rate: float
    frozen_share: float
    n_samples: int


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        [settings.ffprobe_exe, "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return 0.0
    try:
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0) or 0)
    except (ValueError, KeyError):
        return 0.0


def _grab_frame(video: Path, sec: float, out: Path) -> bool:
    r = subprocess.run(
        [settings.ffmpeg_exe, "-y", "-v", "error",
         "-ss", f"{sec:.2f}", "-i", str(video),
         "-frames:v", "1", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def _frame_features(png: Path) -> tuple[float, float, float, float]:
    """(weapon_edge, brightness, saturation, gray_std) for one frame."""
    im = np.asarray(Image.open(png).convert("RGB")).astype(np.float32)
    h, w, _ = im.shape
    roi = im[int(h * 0.66):, int(w * 0.70):]
    g = roi.mean(axis=2)
    edge = float(np.abs(np.diff(g, axis=1)).mean()
                 + np.abs(np.diff(g, axis=0)).mean())
    mx = roi.max(axis=2)
    mn = roi.min(axis=2)
    sat = float(((mx - mn) / (mx + 1)).mean())
    full = im.mean()
    return edge, float(full), sat, float(g.std())


def _motion(a: Path, b: Path) -> float:
    ga = np.asarray(Image.open(a).convert("L").resize((160, 90))).astype(np.float32)
    gb = np.asarray(Image.open(b).convert("L").resize((160, 90))).astype(np.float32)
    return float(np.abs(ga - gb).mean())


def verify_round_clip(video: Path, start_tick: int, end_tick: int,
                      death_tick: int | None, tickrate: int = 64) -> RoundVerdict:
    dur = _probe_duration(video)
    if dur <= 0:
        return RoundVerdict(False, "unprobable duration", 0.0, 0.0, 0)
    alive_end_tick = death_tick if death_tick else end_tick
    alive_end = min((alive_end_tick - start_tick) / tickrate + DEATH_GRACE_S, dur)
    times: list[float] = []
    t = 1.0
    while t < alive_end:
        times.append(t)
        t += SAMPLE_EVERY_S
    if len(times) < MIN_SAMPLES:
        # Short alive window (early death): not enough signal, pass through
        # rather than false-positive on 1-2 knife/flash frames.
        return RoundVerdict(True, f"alive window too short ({len(times)} samples)", 1.0, 0.0, len(times))

    weapon_hits = 0
    weapon_n = 0
    frozen_pairs = 0
    motion_pairs = 0
    prev: Path | None = None
    with tempfile.TemporaryDirectory() as tmp:
        for i, sec in enumerate(times):
            out = Path(tmp) / f"s{i:03d}.png"
            if not _grab_frame(video, sec, out):
                continue
            edge, bright, sat, _std = _frame_features(out)
            if bright >= FLASH_BRIGHT or bright <= SCOPE_DARK or sat <= SMOKE_SAT:
                prev = None  # flashed/smoked/scoped: breaks motion chain too
                continue
            weapon_n += 1
            if edge >= WEAPON_EDGE_PASS:
                weapon_hits += 1
            if prev is not None:
                motion_pairs += 1
                if _motion(prev, out) < FROZEN_DIFF:
                    frozen_pairs += 1
            prev = out

    if weapon_n < MIN_SAMPLES:
        return RoundVerdict(True, f"only {weapon_n} usable samples (flashes/scopes?)", 1.0, 0.0, weapon_n)
    weapon_rate = weapon_hits / weapon_n
    frozen_share = (frozen_pairs / motion_pairs) if motion_pairs else 0.0
    if frozen_share > FROZEN_SHARE_MAX:
        return RoundVerdict(False, f"stuck camera (frozen {frozen_share:.0%})",
                            weapon_rate, frozen_share, weapon_n)
    if weapon_rate < WEAPON_PASS_RATE_MIN:
        return RoundVerdict(False, f"no viewmodel (weapon {weapon_rate:.0%})",
                            weapon_rate, frozen_share, weapon_n)
    return RoundVerdict(True, "ok", weapon_rate, frozen_share, weapon_n)


def death_ticks_by_round(analysis: dict, steam_id: str) -> dict[int, int]:
    kills = analysis.get("kills", [])
    if isinstance(kills, dict):
        kills = list(kills.values())
    deaths: dict[int, int] = {}
    for k in kills:
        if not isinstance(k, dict):
            continue
        try:
            if str(k.get("victimSteamId")) != str(steam_id):
                continue
            rn, t = int(k.get("roundNumber")), int(k.get("tick"))
        except (TypeError, ValueError):
            continue
        if rn not in deaths or t < deaths[rn]:
            deaths[rn] = t
    return deaths


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify POV round clip(s)")
    ap.add_argument("video", help="round-*.mp4 clip or a folder of them")
    ap.add_argument("--start-tick", type=int, default=0)
    ap.add_argument("--end-tick", type=int, default=0)
    ap.add_argument("--death-tick", type=int, default=0)
    ap.add_argument("--tickrate", type=int, default=64)
    args = ap.parse_args()

    p = Path(args.video)
    clips = sorted(p.glob("round-*-tick-*-to-*.mp4")) if p.is_dir() else [p]
    bad = 0
    for clip in clips:
        v = verify_round_clip(clip, args.start_tick, args.end_tick,
                              args.death_tick or None, args.tickrate)
        print(f"  [{'OK' if v.ok else 'BAD'}] {clip.name}: {v.reason} "
              f"(weapon {v.weapon_rate:.0%}, frozen {v.frozen_share:.0%}, n={v.n_samples})")
        bad += not v.ok
    if bad:
        print(f"[VERIFY] {bad}/{len(clips)} clip(s) BAD")
        sys.exit(1)
    print(f"[VERIFY] {len(clips)} clip(s) ok")


if __name__ == "__main__":
    main()
