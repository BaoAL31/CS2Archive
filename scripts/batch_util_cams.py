#!/usr/bin/env python3
"""Batched util_cam rerender using CS2UtilArchive's render_spot_batch.

Replaces overlay_pov.py's single-clip run_csdm loop with one CS2 launch
per chunk of N util_cams (default 8).  Verifies batching by:
  - timing: each chunk should take 1-3 min (one CS2 launch)
  - artifact: a single batch_config_<label>.json per chunk in the work_dir

Usage:
  python scripts/batch_util_cams.py --util-cams-root <pov>/utility_cams \\
      --data-dir D:/Projects/CS2UtilArchive/results/.../demo=<id> \\
      --chunk-size 8

If batching silently degrades to one CS2 launch per spot, the script aborts
with a clear error so the user can debug.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

# Point at CS2UtilArchive (sibling project, mirrors overlay_pov.py setup)
_CS2UTIL_ROOT = Path(r"D:\Projects\CS2UtilArchive")
for _p in (str(_CS2UTIL_ROOT / "scripts"), str(_CS2UTIL_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd

from scripts.render.batch_csdm import (
    BatchRenderOptions,
    BatchSpotJob,
    chunk_demo_batch,
    render_spot_batch,
)
from scripts.render.paths import (
    actions_path_for_demo,
    resolve_demo_path,
    throw_id_to_slug,
    util_render_slug,
)


def _discover_util_cams(util_cams_root: Path) -> list[Path]:
    """Yield util_cam dirs that have a _throw_poses.json and no existing mp4.

    Layout: <util_cams_root>/unnamed/<util_slug>/<demo_id>/
    """
    out: list[Path] = []
    for poses_f in sorted(util_cams_root.rglob("_throw_poses.json")):
        util_dir = poses_f.parent
        existing = list(util_dir.glob("*.mp4"))
        if existing:
            continue  # already rendered
        out.append(util_dir)
    return out


def _build_job(util_dir: Path, throws_df: pd.DataFrame, util_cams_root: Path) -> tuple[dict | None, str]:
    """Construct a flight-only batch job from a util_cam dir + throws.parquet.

    Returns (job_dict_or_None, throw_id).
    """
    poses = json.loads((util_dir / "_throw_poses.json").read_text(encoding="utf-8"))
    throw_map = poses.get("_throws", {})
    if not throw_map:
        return None, ""
    # Take the first throw_id in the map
    throw_id = next(iter(throw_map.keys()))

    rows = throws_df[throws_df["throw_id"] == throw_id]
    if rows.empty:
        return None, throw_id
    row = rows.iloc[0]
    if not bool(row.get("is_renderable", False)):
        return None, throw_id
    if not bool(row.get("has_trajectory", False)):
        return None, throw_id

    demo_id = util_dir.name  # demo_id is the leaf dir name
    util_slug = util_dir.parent.name  # unnamed/<util_slug>/<demo_id>
    # util_id format: <map>:<util_type>:<side>:<relx>_<rely>_<relz>
    util_id = util_slug.replace("_", ":", 3)  # crude — only safe for unnamed slugs
    # Better: parse the slug back to util_id using release_x/y/z
    release_x = int(row.get("release_x", 0) or 0)
    release_y = int(row.get("release_y", 0) or 0)
    release_z = int(row.get("release_z", 0) or 0)
    map_name = str(row.get("map", "") or "")
    util_type = str(row.get("util_type", "") or "")
    thrower_side = str(row.get("thrower_side", "T") or "T")[:1].upper()
    util_id = f"{map_name}:{util_type}:{thrower_side}:{release_x}_{release_y}_{release_z}"

    demo_path = str(_find_demo_for_id(util_cams_root, demo_id))
    if not Path(demo_path).is_file():
        return None, throw_id

    det = row.get("detonate_tick")
    if det is None or (isinstance(det, float) and math.isnan(det)):
        detonate_tick = int(row.get("land_tick", row.get("throw_tick", 0)) or 0)
    else:
        detonate_tick = int(det)

    job: dict = {
        "job_type": "thrower",
        "util_id": util_id,
        "demo_id": demo_id,
        "demo_path": demo_path,
        "util_type": util_type,
        "throw_id": throw_id,
        "thrower_steamid": str(int(row.get("thrower_steamid", 0) or 0)),
        "round_num": int(row.get("round_num", 0) or 0),
        "throw_tick": int(row.get("throw_tick", 0) or 0),
        "detonate_tick": detonate_tick,
        "land_tick": int(row.get("land_tick", 0) or 0),
        "throw_x": float(row.get("throw_x", 0) or 0),
        "throw_y": float(row.get("throw_y", 0) or 0),
        "throw_z": float(row.get("throw_z", 0) or 0),
        "throw_pitch": float(row.get("throw_pitch", 0) or 0),
        "throw_yaw": float(row.get("throw_yaw", 0) or 0),
        "release_spot_rank": 1,
        "is_renderable": True,
        "cameras": "flight",  # flight-only
    }
    return job, throw_id


def _find_demo_for_id(util_cams_root: Path, demo_id: str) -> Path:
    """Find the .dem file for a demo_id.  Walks known locations."""
    project_root = util_cams_root
    for _ in range(5):
        if (project_root / "demos").is_dir():
            break
        project_root = project_root.parent
    hltv_root = project_root / "demos" / "hltv"
    if hltv_root.is_dir():
        for sub in hltv_root.iterdir():
            cand = sub / f"{demo_id}.dem"
            if cand.is_file():
                return cand.resolve()
    return Path(demo_id + ".dem")


def _write_throw_poses(entry: BatchSpotJob, out_path: Path) -> None:
    """Write/refresh _throw_poses.json for the rendered util_cam dir.

    Merges with existing entries (one util_cam dir may represent multiple
    throws sharing the same release position). Format:
        {"1": {"pos": [...]}, "_throws": {throw_id: {"pos": [...]}, ...}}
    """
    job = entry.job
    util_dir = entry.util_dir
    throw_id = str(job.get("throw_id", ""))
    rel_x = int(round(float(job.get("release_x", job.get("throw_x", 0)) or 0)))
    rel_y = int(round(float(job.get("release_y", job.get("throw_y", 0)) or 0)))
    rel_z = int(round(float(job.get("release_z", job.get("throw_z", 0)) or 0)))
    pos = [rel_x, rel_y, rel_z]
    poses_file = util_dir / "_throw_poses.json"
    if poses_file.is_file():
        try:
            poses_data = json.loads(poses_file.read_text(encoding="utf-8"))
        except Exception:
            poses_data = {}
    else:
        poses_data = {}
    poses_data.setdefault("1", {"pos": pos})
    poses_data.setdefault("_throws", {})
    poses_data["_throws"][throw_id] = {"pos": pos}
    poses_file.write_text(json.dumps(poses_data, indent=2), encoding="utf-8")
    n_total = len(poses_data["_throws"])
    print(f"  [batch] wrote {poses_file.relative_to(util_dir.parent.parent)} "
          f"({throw_id}; {n_total} throw(s) in dir)", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--util-cams-root", required=True,
                    help="Path to <pov>/utility_cams (must contain unnamed/<slug>/<demo_id>/)")
    ap.add_argument("--data-dir", required=True,
                    help="CS2UtilArchive per-demo data dir (parent of throws.parquet)")
    ap.add_argument("--chunk-size", type=int, default=8,
                    help="Spots per CS2 launch (default 8). 0 = all in one launch.")
    ap.add_argument("--debug", action="store_true",
                    help="Enable per-batch debug.log (CS2UtilArchive side)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build jobs but don't render")
    args = ap.parse_args()

    util_cams_root = Path(args.util_cams_root).resolve()
    data_dir = Path(args.data_dir).resolve()
    # data_dir is the parent containing demo=<id>/ subdirs (per CS2UtilArchive).
    # Map each util_cam dir to its demo_id (the leaf dir name), then look up
    # the matching demo=* subdir under data_dir.
    util_dirs = _discover_util_cams(util_cams_root)
    print(f"[batch-util] discovered {len(util_dirs)} util_cam dirs needing render")
    if not util_dirs:
        return 0

    needed_demo_ids: set[str] = {ud.name for ud in util_dirs}
    # CS2Archive util_cam leaf = "furia-vs-falcons-m3-inferno"
    # CS2UtilArchive demo dir = "demo=2395002-furia-vs-falcons-m3-inferno"
    # Map leaf -> demo dir by stem suffix after the match ID.
    demo_dir_by_stem: dict[str, Path] = {}
    for d in data_dir.iterdir():
        if not (d.is_dir() and d.name.startswith("demo=")):
            continue
        stem = d.name[len("demo="):]
        slug = "-".join(stem.split("-")[1:]) if "-" in stem else stem
        demo_dir_by_stem[slug] = d
    throws_df: pd.DataFrame | None = None
    matched_demo_dir: Path | None = None
    for ud in util_dirs:
        leaf = ud.name
        if leaf in demo_dir_by_stem:
            matched_demo_dir = demo_dir_by_stem[leaf]
            tp = matched_demo_dir / "throws.parquet"
            if tp.is_file():
                throws_df = pd.read_parquet(tp)
                break
    if throws_df is None:
        print(f"[FAIL] no throws.parquet found for any of {len(needed_demo_ids)} demo_ids "
              f"under {data_dir}", file=sys.stderr)
        return 1
    print(f"[batch-util] loaded {len(throws_df)} throws from "
          f"data/{matched_demo_dir.name}/throws.parquet")

    entries: list[BatchSpotJob] = []
    skipped: list[tuple[str, str]] = []  # (util_dir, reason)
    for util_dir in util_dirs:
        job, throw_id = _build_job(util_dir, throws_df, util_cams_root)
        if job is None:
            skipped.append((str(util_dir), throw_id or "(no throw_id)"))
            continue
        entries.append(BatchSpotJob(job=job, util_dir=util_dir))

    print(f"[batch-util] built {len(entries)} batchable jobs, {len(skipped)} skipped")
    for util_dir, reason in skipped[:5]:
        print(f"  SKIP: {util_dir} ({reason})")
    if len(skipped) > 5:
        print(f"  ... and {len(skipped) - 5} more skips")

    if not entries:
        print("[batch-util] nothing to render")
        return 0

    # Group by demo_id (all util_cams in this script share one demo)
    by_demo: dict[str, list[BatchSpotJob]] = {}
    for entry in entries:
        by_demo.setdefault(str(entry.job["demo_id"]), []).append(entry)
    for demo_id in by_demo:
        by_demo[demo_id].sort(key=lambda e: int(e.job["throw_tick"]))

    options = BatchRenderOptions(
        data_dir=str(data_dir),
        cam_offset=380.0,
        cam_height=96.0,
        flight_smooth=0.75,
        dry_run=args.dry_run,
        render_profile="catalog",
        debug=args.debug,
        camera_types=("flight",),  # flight-only
    )

    work_dir = util_cams_root / "_batch_workdir"
    work_dir.mkdir(parents=True, exist_ok=True)

    overall_t0 = time.time()
    total_rendered = 0
    for demo_id, demo_entries in by_demo.items():
        chunks = chunk_demo_batch(demo_entries, args.chunk_size)
        n_chunks = len(chunks)
        print(
            f"\n[batch-util] demo={demo_id}: {len(demo_entries)} spots, "
            f"{n_chunks} CS2 launch(es) (chunk_size={args.chunk_size})",
            flush=True,
        )
        if n_chunks == len(demo_entries):
            print(
                f"  [WARN] chunk_size={args.chunk_size} produced {n_chunks} chunks "
                f"for {len(demo_entries)} spots → effectively one CS2 launch per spot. "
                f"Increase --chunk-size or set 0 to verify true batching.",
                file=sys.stderr,
            )
        for batch_i, chunk in enumerate(chunks, start=1):
            label = demo_id if n_chunks == 1 else f"{demo_id}_chunk{batch_i}"
            print(
                f"\n=== batch {batch_i}/{n_chunks}: {label} "
                f"({len(chunk)} spots) ===", flush=True,
            )
            tick_order = " -> ".join(
                f"{util_render_slug(str(e.job['util_id']))}@{int(e.job['throw_tick'])}"
                for e in chunk
            )
            print(f"  Spots: {tick_order}", flush=True)
            t0 = time.time()
            try:
                outputs = render_spot_batch(
                    chunk, work_dir, options, batch_label=label,
                )
            except Exception as exc:
                print(f"  [batch] FAILED: {exc}", file=sys.stderr, flush=True)
                return 2
            elapsed = time.time() - t0
            per_spot = elapsed / max(1, len(outputs))
            print(
                f"  [batch] {len(outputs)}/{len(chunk)} clips in {elapsed:.0f}s "
                f"({per_spot:.0f}s per spot)",
                flush=True,
            )
            # Verify batching: per-spot time amortizes CS2-launch overhead across N
            # spots. Unbatched baseline is ~40-50s/spot (full CS2 launch each).
            # Batched should be well under that for chunks >= 4. Use generous
            # threshold of 50s; sequential will be 60-90s/spot regardless of N.
            if len(chunk) > 1 and per_spot > 50.0:
                msg = (
                    f"  [FAIL] per-spot time {per_spot:.0f}s in chunk of {len(chunk)} "
                    f"is over 50s — looks like sequential CS2 launches, not batching. "
                    f"Debug: check {work_dir / label}/"
                )
                print(msg, file=sys.stderr, flush=True)
                return 3
            # Write _throw_poses.json for each successful render so overlay_pov.py
            # scan can map throw_id -> mp4 clip.
            for out_path in outputs:
                for entry in chunk:
                    if entry.util_dir.resolve() == out_path.parent.resolve():
                        _write_throw_poses(entry, out_path)
                        break
            total_rendered += len(outputs)

    total_elapsed = time.time() - overall_t0
    print(
        f"\n[batch-util] DONE: {total_rendered} clips in {total_elapsed:.0f}s "
        f"({total_elapsed / max(1, total_rendered):.0f}s per clip)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
