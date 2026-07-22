"""Rename/copy sequence-*-tick-*.mp4 to seg-NNN by matching edit_timeline tick ranges."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from _pathsetup import ensure

ensure()

MIN_BYTES = 1_048_576


def main() -> int:
    run_dir = ROOT / "renders" / "hl-team_teses vs team_SVNONETHREE - cache"
    if len(sys.argv) > 1:
        run_dir = Path(sys.argv[1])
    edit_path = run_dir / "edit_timeline.json"
    seg_dir = run_dir / "segments"
    segs = json.loads(edit_path.read_text(encoding="utf-8"))["segments"]

    by_ticks: dict[tuple[int, int], Path] = {}
    for p in seg_dir.glob("sequence-*-tick-*-to-*.mp4"):
        m = re.match(r"sequence-\d+-tick-(\d+)-to-(\d+)", p.name)
        if m:
            by_ticks[(int(m.group(1)), int(m.group(2)))] = p

    wrote = 0
    missing: list[tuple[int, tuple[int, int]]] = []
    for i, s in enumerate(segs, 1):
        key = (int(s["start_tick"]), int(s["end_tick"]))
        dest = seg_dir / (
            f"seg-{i:03d}-pov-{s['pov_steam_id']}-tick-{key[0]}-to-{key[1]}.mp4"
        )
        if dest.exists() and dest.stat().st_size >= MIN_BYTES:
            continue
        src = by_ticks.get(key)
        if src is None:
            missing.append((i, key))
            continue
        shutil.copy2(src, dest)
        wrote += 1
        print(f"[OK] {src.name} -> {dest.name}")

    have = sum(
        1
        for i, s in enumerate(segs, 1)
        if (seg_dir / f"seg-{i:03d}-pov-{s['pov_steam_id']}-tick-{s['start_tick']}-to-{s['end_tick']}.mp4").exists()
    )
    print(f"Done: wrote {wrote}, have {have}/{len(segs)} seg files, missing {len(missing)}")
    for i, key in missing:
        print(f"  missing seg {i}: ticks {key[0]}-{key[1]}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
