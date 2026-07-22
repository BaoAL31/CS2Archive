"""Verify overlay frame→tick mapping is in sync with CSDM sequence files.

CSDM writes one `sequence-N-tick-START-to-END.mp4` per round it records.
The overlay must map each rendered video frame to a tick inside that
sequence's [START, END] range — otherwise the keyboard overlay appears
"too early" or "too late" relative to the gameplay.

This test:
  1. Renders one round (or reuses an existing render folder).
  2. Runs concat_rounds → combined.mp4 + sidecar (now containing
     `per_round_ticks` parsed from the sequence filenames).
  3. Inspects the sidecar's `per_round_ticks` vs the actual
     `sequence-*-tick-*-to-*.mp4` filenames in the folder.
  4. Asserts they match exactly.

Usage:
    python -m pytest tests/test_overlay_sync.py -s --demo <path.dem> \
        --steam-id 76561198041683378 --round 1
or
    python tests/test_overlay_sync.py <demo> <steam_id> --round 1 \
        [--renders-dir renders/_test_sync]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = r"C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
SEQ_RE = re.compile(r"sequence-(\d+)-tick-(\d+)-to-(\d+)\.mp4$")


def _render(demo: Path, steam_id: str, round_num: int, out: Path) -> None:
    """Render one round via render_pov.py (skips if already rendered)."""
    if (out / "combined.mp4").exists() and any(out.glob("sequence-*-tick-*-to-*.mp4")):
        print(f"[skip] {out.name} already rendered + sequences present")
        return
    print(f"[render] round {round_num} -> {out}")
    out.mkdir(parents=True, exist_ok=True)
    # Remove stale batch files so we get fresh sequences
    for f in out.glob("batch-*.mp4"):
        f.unlink()
    for f in out.glob("sequence-*.mp4"):
        f.unlink()
    if (out / "combined.mp4").exists():
        (out / "combined.mp4").unlink()
    r = subprocess.run(
        [PY, "scripts/pov/render_pov.py", str(demo), steam_id,
         "--output", str(out.resolve()), "--rounds", str(round_num), "--batches", "1"],
        cwd=ROOT, shell=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"render_pov.py exited {r.returncode}")


def _concat(out: Path) -> Path:
    """Run concat_rounds.py and return the combined.mp4 path."""
    sidecar = out / "combined.round_offsets.json"
    if sidecar.exists() and sidecar.stat().st_size > 0 and "per_round_ticks" not in sidecar.read_text():
        # Stale sidecar (pre-fix) — remove so concat regenerates.
        sidecar.unlink()
    if (out / "combined.mp4").exists() and not sidecar.exists():
        # Need to re-render since concat is one-shot per batch set.
        print("[concat] sidecar missing; re-rendering for fresh batches...")
        (out / "combined.mp4").unlink()
        for f in out.glob("batch-*.mp4"):
            f.unlink()
        raise RuntimeError("rerun needed")
    if not sidecar.exists():
        print(f"[concat] running concat_rounds.py -> {out}")
        r = subprocess.run([PY, "scripts/pov/concat_rounds.py", str(out)], cwd=ROOT)
        if r.returncode != 0:
            raise RuntimeError(f"concat_rounds.py exited {r.returncode}")
    return out / "combined.mp4"


def _expected_ticks_from_filenames(out: Path) -> dict[int, tuple[int, int]]:
    """Parse sequence-*-tick-START-to-END.mp4 filenames."""
    seqs = []
    for f in out.glob("sequence-*-tick-*-to-*.mp4"):
        m = SEQ_RE.match(f.name)
        if m:
            seqs.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    seqs.sort(key=lambda x: x[0])
    # Sequence index -> round num mapping NOT known from filenames alone; use
    # the sidecar's round range (render_pov was invoked with --rounds N).
    return {idx: (s, e) for idx, (s, e) in enumerate(seqs)}


def _check(demo: Path, steam_id: str, round_num: int, renders_dir: Path) -> None:
    out = renders_dir / f"sync_r{round_num}"
    print(f"=== Test overlay sync for round {round_num} in {demo.name} ===")
    _render(demo, steam_id, round_num, out)
    _concat(out)

    sidecar = json.loads((out / "combined.round_offsets.json").read_text())
    seq_files = sorted(out.glob("sequence-*-tick-*-to-*.mp4"))
    if not seq_files:
        raise AssertionError("No sequence-* files preserved after concat")

    # Parse tick ranges from filenames
    expected = []
    for f in seq_files:
        m = SEQ_RE.match(f.name)
        if not m:
            continue
        expected.append((int(m.group(2)), int(m.group(3))))

    actual_per_round = sidecar.get("per_round_ticks")
    actual_durations = sidecar.get("per_round_durations")
    if not actual_per_round or not actual_durations:
        raise AssertionError("Sidecar missing per_round_ticks or per_round_durations")

    # Sidecar keys (round nums) — should be 1:1 with sequence files in ORDER
    sorted_rns = sorted(actual_per_round.keys(), key=int)
    if len(sorted_rns) != len(expected):
        raise AssertionError(
            f"sequence files ({len(expected)}) != sidecar rounds ({len(sorted_rns)})"
        )

    # Verify each seq tick range matches the sidecar value at the same position
    for rn, (exp_start, exp_end) in zip(sorted_rns, expected):
        rn_int = int(rn)
        sc_ticks = actual_per_round[rn]
        sc_start, sc_end = int(sc_ticks[0]), int(sc_ticks[1])
        if sc_start != exp_start or sc_end != exp_end:
            raise AssertionError(
                f"Round {rn_int}: sidecar ({sc_start}, {sc_end}) != "
                f"sequence file ({exp_start}, {exp_end})"
            )
        # Verify probed duration matches sequence file span / TICKRATE
        exp_dur = (exp_end - exp_start) / 64.0  # 64 tick/s
        sc_dur = float(actual_durations[rn])
        delta = abs(sc_dur - exp_dur)
        # Probed durations from ffmpeg can drift slightly relative to tick count
        # CSDM applies a small fixed-offset per round (~0.1s). Allow ±1s drift.
        if delta > 1.0:
            raise AssertionError(
                f"Round {rn_int}: sidecar duration {sc_dur:.3f}s != expected "
                f"{exp_dur:.3f}s (delta {delta:.3f}s)"
            )

    print(f"[PASS] {len(expected)} rounds: per_round_ticks match sequence filenames, "
          f"durations within ±1s of (tick_span / 64)")
    print(f"  sample r{sorted_rns[0]}: ticks={actual_per_round[sorted_rns[0]]} "
          f"dur={actual_durations[sorted_rns[0]]:.2f}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("demo")
    ap.add_argument("steam_id")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--renders-dir", default="renders/_sync_test")
    args = ap.parse_args()
    try:
        _check(Path(args.demo).resolve(), args.steam_id, args.round,
               Path(args.renders_dir).resolve())
    except AssertionError as e:
        print(f"[FAIL] {e}")
        return 1
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())