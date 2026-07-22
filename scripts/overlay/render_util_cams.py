#!/usr/bin/env python3
"""Render util_cam clips for a player's POV (prep + render in one pass).

Two phases:
  1. PREP — load throws.parquet, filter by player steamid, group by release
     position, create util_cam dirs + _throw_poses.json for missing throws.
  2. RENDER — discover util_cam dirs needing render (no .mp4 yet), call
     CS2UtilArchive's render_spot_batch in batched chunks.

Idempotent: re-running on a fully-rendered POV is a no-op. Use --prepare-only
or --render-only to run a single phase.

Usage:
  # Full prep + render for one player on one map (NiKo on inferno, all sides):
  python scripts/render_util_cams.py \\
      --util-cams-root D:/Projects/CS2Archive/renders/pov-furia-vs-falcons-m3-inferno_76561198041683378_full/utility_cams \\
      --data-dir D:/Projects/CS2UtilArchive/results/iem_cologne_major_2026/data \\
      --steamid 76561198041683378

  # All players (no filter):
  python scripts/render_util_cams.py --util-cams-root ... --data-dir ...

  # Just prep dirs (no CS2 launch):
  python scripts/render_util_cams.py --util-cams-root ... --data-dir ... --prepare-only

  # Just render existing dirs (skip prep):
  python scripts/render_util_cams.py --util-cams-root ... --data-dir ... --render-only

Batching verified by:
  - timing: each chunk should take 1-3 min (one CS2 launch)
  - artifact: a single batch_config_<label>.json per chunk in _batch_workdir
  - per-spot time: <50s indicates true batching; >60s means sequential fallback
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

# Point at CS2UtilArchive (sibling project, mirrors overlay_pov.py setup)
_CS2UTIL_ROOT = Path(r"D:\Projects\CS2Archive").parent / "CS2UtilArchive"
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
from scripts.render.paths import util_render_slug, clip_name_for_cameras
from scripts.build_player_manifest import build_manifest


# ---------------------------------------------------------------------------
# Phase 1: PREP — create util_cam dirs + _throw_poses.json
# ---------------------------------------------------------------------------

def _prepare_util_cams(
    steam_id: str,
    demo_id: str,
    data_dir: Path,
    demos_dir: Path,
    util_cams_root: Path,
) -> tuple[int, int, int]:
    """Create util_cam dirs + _throw_poses.json via canonical CS2UtilArchive manifest builder.

    ``data_dir`` is the ROOT data dir (parent of ``demo=*`` subdirs).
    ``build_manifest()`` receives it as-is and reconstructs the path:
    ``data_dir / f"demo={demo_id}" / "throws.parquet"``.

    Default cameras: smoke/fire=flight,detonate, others=flight.

    Returns (created, skipped_already_rendered, total_entries).
    """
    manifest = build_manifest(
        steam_id=steam_id,
        demo_id=demo_id,
        data_dir=data_dir,
        demos_dir=demos_dir,
        cameras_smoke="flight,detonate",
        cameras_fire="flight,detonate",
        cameras_other="flight",
    )
    entries = manifest["entries"]
    if not entries:
        print(f"[prep] no renderable throws for steamid={steam_id}")
        return 0, 0, 0

    # Load per-throw map name from the parquet (manifest entries lack it).
    map_by_throw: dict[str, str] = {}
    tp = data_dir / f"demo={demo_id}" / "throws.parquet"
    if tp.is_file():
        try:
            tdf = pd.read_parquet(tp)
            if "map" in tdf.columns:
                for _, r in tdf.iterrows():
                    map_by_throw[str(r["throw_id"])] = str(r["map"])
        except Exception:
            pass

    # Group throws by util_id (map:type:side:land coords) to mirror
    # CS2UtilArchive's util_id-keyed folder architecture: one dir per
    # util_id (no match id), with multiple throw clips inside it.
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        uid = _util_id_for_entry(entry, map_by_throw)
        groups.setdefault(uid, []).append(entry)

    created = 0
    skipped = 0
    for uid, grp in groups.items():
        uid_slug = util_render_slug(uid)
        util_dir = util_cams_root / "unnamed" / uid_slug
        # A util_id group needs render if ANY of its throws lacks a clip.
        needs: list[dict] = []
        for entry in grp:
            cam = entry.get("cameras") or _cameras_for_type(entry.get("util_type", ""))
            clip = util_dir / f"{clip_name_for_cameras(cam, entry['throw_id'])}.mp4"
            if not (clip.is_file() and clip.stat().st_size > 1_000_000):
                needs.append(entry)
        if not needs:
            skipped += 1
            continue
        util_dir.mkdir(parents=True, exist_ok=True)
        poses_data = _build_poses_json(grp, demo_id)
        (util_dir / "_throw_poses.json").write_text(
            json.dumps(poses_data, indent=2), encoding="utf-8"
        )
        created += 1
        for entry in needs:
            print(f"  [prep] {uid_slug} ({entry.get('util_type','')}, "
                  f"{entry.get('cameras','flight')}) throw {entry['throw_id']}", flush=True)

    return created, skipped, len(entries)


# ---------------------------------------------------------------------------
# Phase 2: RENDER — discover dirs needing render, call CS2UtilArchive
# ---------------------------------------------------------------------------

def _discover_util_cams(util_cams_root: Path) -> list[Path]:
    """Yield util_cam dirs that still need rendering.

    A dir needs rendering if ANY throw listed in its ``_throw_poses.json`` is
    missing its EXPECTED clip (named per the dir's ``_cameras`` field), e.g.
    ``flight_detonate_<slug>.mp4`` for smokes/molotov. We must check the
    SPECIFIC expected clip, NOT just "any *.mp4 exists": a dir can hold the
    separate ``flight`` + ``detonate`` clips while its COMBINED
    ``flight_detonate`` clip failed to render. Treating "has any mp4" as
    "already rendered" skips the dir forever and the combined clip is never
    produced — which is exactly how smokes ended up showing only the
    standalone detonate in the overlay.
    """
    out: list[Path] = []
    for poses_f in sorted(util_cams_root.rglob("_throw_poses.json")):
        util_dir = poses_f.parent
        try:
            poses = json.loads(poses_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cam = poses.get("_cameras") or "flight"
        throw_map = poses.get("_throws", {})
        if not throw_map:
            continue
        needs_render = False
        for tid in throw_map:
            clip = util_dir / f"{clip_name_for_cameras(cam, tid)}.mp4"
            if not (clip.is_file() and clip.stat().st_size > 1_000_000):
                needs_render = True
                break
        if needs_render:
            out.append(util_dir)
    return out


def _find_demo_for_id(util_cams_root: Path, demo_id: str) -> Path:
    """Find the .dem file for a demo_id.  Walks known locations.

    Accepts both full slug ("2395002-furia-vs-falcons-m3-inferno") and short
    slug ("furia-vs-falcons-m3-inferno"). Tries both, with the full slug
    first since it's more specific.
    """
    import re
    project_root = util_cams_root
    for _ in range(5):
        if (project_root / "demos").is_dir():
            break
        project_root = project_root.parent
    hltv_root = project_root / "demos" / "hltv"
    if hltv_root.is_dir():
        # Try the full demo_id first, then strip a leading "<digits>-" match_id prefix
        candidates = [demo_id]
        m = re.match(r"^\d+-(.+)$", demo_id)
        if m:
            candidates.append(m.group(1))
        for cand_id in candidates:
            for sub in hltv_root.iterdir():
                cand = sub / f"{cand_id}.dem"
                if cand.is_file():
                    return cand.resolve()
    return Path(demo_id + ".dem")


def _cameras_for_type(util_type: str) -> str:
    """Canonical camera set per util type (matches CS2UtilArchive)."""
    return "flight,detonate" if str(util_type).lower() in ("smoke", "fire", "molotov", "incendiary") else "flight"


def _util_id_for_row(row: dict) -> str:
    """util_id = map:type:side:land_x_land_y_land_z (no match id)."""
    map_name = str(row.get("map") or row.get("map_name") or "de_anubis")
    util_type = str(row.get("util_type", "")).lower()
    side = str(row.get("thrower_side", "T") or "T").upper()
    lx = float(row.get("land_x", 0) or 0)
    ly = float(row.get("land_y", 0) or 0)
    lz = float(row.get("land_z", 0) or 0)
    return f"{map_name}:{util_type}:{side}:{int(round(lx))}_{int(round(ly))}_{int(round(lz))}"


def _util_id_for_entry(entry: dict, map_by_throw: dict[str, str]) -> str:
    """util_id for a manifest entry (map resolved from throws.parquet)."""
    tid = str(entry["throw_id"])
    map_name = map_by_throw.get(tid) or "de_anubis"
    util_type = str(entry.get("util_type", "")).lower()
    side = str(entry.get("thrower_side", "T") or "T").upper()
    land = entry.get("land_pos") or entry.get("release_pos") or [0, 0, 0]
    lx, ly, lz = float(land[0]), float(land[1]), float(land[2])
    return f"{map_name}:{util_type}:{side}:{int(round(lx))}_{int(round(ly))}_{int(round(lz))}"


def _build_poses_json(grp: list[dict], demo_id: str) -> dict:
    """Aggregate throws in a util_id group into one _throw_poses.json."""
    data: dict = {
        "_throws": {},
        "_cameras": (grp[0].get("cameras") or "flight"),
        "_demo_id": demo_id,
    }
    for i, entry in enumerate(grp, start=1):
        pos = [
            int(round(float(entry.get("release_x", 0) or 0))),
            int(round(float(entry.get("release_y", 0) or 0))),
            int(round(float(entry.get("release_z", 0) or 0))),
        ]
        data[str(i)] = {"pos": pos}
        data["_throws"][str(entry["throw_id"])] = {"pos": pos}
    return data


def _build_job(util_dir: Path, throw_id: str, throws_df: pd.DataFrame, util_cams_root: Path) -> tuple[dict | None, str]:
    """Construct a batch job for one throw_id inside a util_id-keyed dir.

    Dir naming: unnamed/<util_id_slug>/ (no match id). Multiple throw_ids
    may share the dir (same landing spot, different entity/segment).
    """
    rows = throws_df[throws_df["throw_id"] == throw_id]
    if rows.empty:
        return None, throw_id
    row = rows.iloc[0]
    if not bool(row.get("is_renderable", False)):
        return None, throw_id
    if not bool(row.get("has_trajectory", False)):
        return None, throw_id

    demo_id = str(row.get("demo_id", "") or "")
    if not demo_id:
        try:
            poses = json.loads((util_dir / "_throw_poses.json").read_text(encoding="utf-8"))
            demo_id = str(poses.get("_demo_id", "") or "")
        except Exception:
            demo_id = ""
    util_id = _util_id_for_row(row)
    cameras = row.get("cameras") or _cameras_for_type(row.get("util_type", ""))

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
        "util_type": str(row.get("util_type", "") or ""),
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
        # cluster_x/y/z = target of the throw (orbit camera anchor). Falls back
        # to land (grenade rest pos) then release (thrower pos) for smoke/fire/HE.
        "cluster_x": float(row.get("land_x", row.get("detonate_x", row.get("release_x", 0))) or 0),
        "cluster_y": float(row.get("land_y", row.get("detonate_y", row.get("release_y", 0))) or 0),
        "cluster_z": float(row.get("land_z", row.get("detonate_z", row.get("release_z", 0))) or 0),
        "is_renderable": True,
        "cameras": cameras,
    }
    return job, throw_id


def _write_throw_poses(entry: BatchSpotJob, out_path: Path) -> None:
    """Write/refresh _throw_poses.json for the rendered util_cam dir."""
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


def _resolve_throws_parquet(
    data_dir: Path,
    util_cams_root: Path,
    demo_id: str | None = None,
) -> tuple[pd.DataFrame | None, Path | None]:
    """Find throws.parquet matching the util_cam dir naming.

    CS2Archive util_cam leaf = throw_id slug like "2395002-furia-vs-falcons-m3-inferno_e142_s1"
    (or legacy "furia-vs-falcons-m3-inferno").
    CS2UtilArchive demo dir = "demo=2395002-furia-vs-falcons-m3-inferno"

    Strategy:
      1. If --demo-id is given, look up demo=<id>/throws.parquet directly.
      2. Otherwise, extract demo_id from existing util_cam dir slugs
         (everything before _e<digits>_s<digits>), then look up matching demo=* dir.
    """
    import re
    demo_dir_by_stem: dict[str, Path] = {}
    for d in data_dir.iterdir():
        if not (d.is_dir() and d.name.startswith("demo=")):
            continue
        stem = d.name[len("demo="):]
        demo_dir_by_stem[stem] = d
        # Also map short slug
        slug = "-".join(stem.split("-")[1:]) if "-" in stem else stem
        demo_dir_by_stem[slug] = d

    # 1. Explicit --demo-id (works on first run with empty util_cams_root)
    if demo_id:
        if demo_id in demo_dir_by_stem:
            tp = demo_dir_by_stem[demo_id] / "throws.parquet"
            if tp.is_file():
                return pd.read_parquet(tp), demo_dir_by_stem[demo_id]
        # Also try short slug form
        m = re.match(r"^\d+-(.+)$", demo_id)
        if m:
            short = m.group(1)
            if short in demo_dir_by_stem:
                tp = demo_dir_by_stem[short] / "throws.parquet"
                if tp.is_file():
                    return pd.read_parquet(tp), demo_dir_by_stem[short]

    # 2. Fallback: discover from existing util_cam dirs
    util_dirs = _discover_util_cams(util_cams_root)
    needed_demo_ids: set[str] = set()
    for ud in util_dirs:
        leaf = ud.name
        # Try throw_id slug pattern: <demo_id>_e<digits>_s<digits>
        m = re.match(r"^(.+)_e\d+_s\d+$", leaf)
        if m:
            needed_demo_ids.add(m.group(1))
        else:
            # Legacy short slug
            needed_demo_ids.add(leaf)

    for leaf in needed_demo_ids:
        if leaf in demo_dir_by_stem:
            matched_demo_dir = demo_dir_by_stem[leaf]
            tp = matched_demo_dir / "throws.parquet"
            if tp.is_file():
                return pd.read_parquet(tp), matched_demo_dir
    return None, None


def _render_util_cams(
    util_cams_root: Path,
    data_dir: Path,
    chunk_size: int,
    dry_run: bool,
    debug: bool,
    demo_id: str | None = None,
) -> int:
    """Render util_cam dirs needing render via CS2UtilArchive render_spot_batch.

    Each util_id dir may hold multiple throw clips; we expand to one
    BatchSpotJob per throw whose clip is missing, sharing the util_dir.
    """
    util_dirs = _discover_util_cams(util_cams_root)
    print(f"[render] discovered {len(util_dirs)} util_cam dirs")
    if not util_dirs:
        return 0

    throws_df, matched_demo_dir = _resolve_throws_parquet(data_dir, util_cams_root, demo_id)
    if throws_df is None:
        print(f"[FAIL] no throws.parquet found for demo_id under {data_dir}",
              file=sys.stderr)
        return 1
    print(f"[render] loaded {len(throws_df)} throws from "
          f"data/{matched_demo_dir.name}/throws.parquet")

    jobs_by_dir: dict[Path, list[BatchSpotJob]] = {}
    skipped = 0
    for util_dir in util_dirs:
        poses_f = util_dir / "_throw_poses.json"
        if not poses_f.is_file():
            continue
        try:
            poses = json.loads(poses_f.read_text(encoding="utf-8"))
        except Exception:
            continue
        throw_map = poses.get("_throws", {})
        if not throw_map:
            continue
        for tid in throw_map:
            cam = poses.get("_cameras") or "flight"
            clip = util_dir / f"{clip_name_for_cameras(cam, tid)}.mp4"
            if clip.is_file() and clip.stat().st_size > 1_000_000:
                skipped += 1
                continue
            job, _ = _build_job(util_dir, tid, throws_df, util_cams_root)
            if job is None:
                continue
            jobs_by_dir.setdefault(util_dir, []).append(BatchSpotJob(job=job, util_dir=util_dir))

    entries: list[BatchSpotJob] = [j for js in jobs_by_dir.values() for j in js]
    print(f"[render] built {len(entries)} batchable jobs, {skipped} skipped (already rendered)")
    if not entries:
        print("[render] nothing to render")
        return 0

    by_demo: dict[str, list[BatchSpotJob]] = {}
    for entry in entries:
        by_demo.setdefault(str(entry.job["demo_id"]), []).append(entry)
    for d in by_demo:
        by_demo[d].sort(key=lambda e: int(e.job["throw_tick"]))

    options = BatchRenderOptions(
        data_dir=str(data_dir),
        cam_offset=380.0,
        cam_height=96.0,
        flight_smooth=0.75,
        dry_run=dry_run,
        render_profile="catalog",
        debug=debug,
        # Per-job cameras field drives actual selection; this is the max set.
        camera_types=("throw", "flight", "orbit", "victims"),
    )

    work_dir = util_cams_root / "_batch_workdir"
    work_dir.mkdir(parents=True, exist_ok=True)

    overall_t0 = time.time()
    total_rendered = 0
    for demo_id, demo_entries in by_demo.items():
        chunks = chunk_demo_batch(demo_entries, chunk_size)
        n_chunks = len(chunks)
        print(
            f"\n[render] demo={demo_id}: {len(demo_entries)} spots, "
            f"{n_chunks} CS2 launch(es) (chunk_size={chunk_size})",
            flush=True,
        )
        if n_chunks == len(demo_entries):
            print(
                f"  [WARN] chunk_size={chunk_size} produced {n_chunks} chunks "
                f"for {len(demo_entries)} spots → one CS2 launch per spot. "
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
            if len(chunk) > 1 and per_spot > 50.0:
                msg = (
                    f"  [FAIL] per-spot time {per_spot:.0f}s in chunk of {len(chunk)} "
                    f"looks like sequential CS2 launches, not batching. "
                    f"Debug: check {work_dir / label}/"
                )
                print(msg, file=sys.stderr, flush=True)
                return 3
            for out_path in outputs:
                for entry in chunk:
                    if entry.util_dir.resolve() == out_path.parent.resolve():
                        _write_throw_poses(entry, out_path)
                        break
            total_rendered += len(outputs)

    total_elapsed = time.time() - overall_t0
    print(
        f"\n[render] DONE: {total_rendered} clips in {total_elapsed:.0f}s "
        f"({total_elapsed / max(1, total_rendered):.0f}s per clip)",
        flush=True,
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Prepare util_cam dirs via CS2UtilArchive canonical manifest + render.",
    )
    ap.add_argument("--util-cams-root", required=True,
                    help="Path to <pov>/utility_cams (must contain unnamed/<util_id_slug>/)")
    ap.add_argument("--data-dir", required=True,
                    help="CS2UtilArchive per-demo data dir (parent of throws.parquet)")
    ap.add_argument("--steamid", type=int, default=None,
                    help="Filter throws.parquet by thrower_steamid")
    ap.add_argument("--demo-id", required=True,
                    help="Full demo slug like '2395002-furia-vs-falcons-m2-anubis'")
    ap.add_argument("--demos-dir", required=True,
                    help="Parent dir of extracted .dem folders")
    ap.add_argument("--chunk-size", type=int, default=0,
                    help="Spots per CS2 launch (default 0 = all in one launch).")
    ap.add_argument("--prepare-only", action="store_true",
                    help="Only create util_cam dirs + _throw_poses.json, skip rendering.")
    ap.add_argument("--render-only", action="store_true",
                    help="Skip prep, only render existing dirs needing render.")
    ap.add_argument("--debug", action="store_true",
                    help="Enable per-batch debug.log (CS2UtilArchive side)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build jobs but don't render")
    args = ap.parse_args()

    if args.prepare_only and args.render_only:
        print("[FAIL] --prepare-only and --render-only are mutually exclusive", file=sys.stderr)
        return 1

    util_cams_root = Path(args.util_cams_root).resolve()
    data_dir = Path(args.data_dir).resolve()
    demos_dir = Path(args.demos_dir).resolve()

    # Phase 1: PREP — use canonical manifest builder
    if not args.render_only:
        print(f"[prep] building manifest via CS2UtilArchive build_manifest()")
        print(f"[prep] steamid={args.steamid} demo={args.demo_id}")
        created, already_rendered, total = _prepare_util_cams(
            steam_id=str(args.steamid),
            demo_id=args.demo_id,
            data_dir=data_dir,
            demos_dir=demos_dir,
            util_cams_root=util_cams_root,
        )
        print(f"[prep] {created} new dirs, {already_rendered} already rendered, "
              f"{total} throws matched")

    if args.prepare_only:
        return 0

    # Phase 2: RENDER
    return _render_util_cams(
        util_cams_root, data_dir, args.chunk_size, args.dry_run, args.debug, args.demo_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
