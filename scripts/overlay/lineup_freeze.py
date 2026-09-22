"""Once-per-lineup PiP gating + freeze-frame pre-pass for non-straightforward throws.

Canon pipeline (``overlay_pov.run_overlay``) integration:

1. **Dedupe** — a POV player repeats lineups across rounds. PiP shows each
   unique lineup once: cluster the player's throws on release position
   (``release_x/y/z``) per util type with CS2UtilArchive's release-spot
   tolerances (smoke/fire 96u, flash/HE 128u) and keep the earliest throw
   per cell. Applied both when rendering flight clips and when counting
   expected clips so the hard-fail validation stays honest.
2. **Straightforwardness** — wraps CS2UtilArchive's ``classify_throw``
   (``scripts/render/volumes.py``): LOS eye->detonate, same air volume,
   exit distance, range, rise. Fail-safe: missing mesh / trajectories /
   scipy → straightforward (no freeze, PiP still shows).
3. **Freeze pre-pass** — for each deduped NON-straightforward throw, hold
   the lineup aim frame (motion-aware anchor via ``freeze_anchor_tick``,
   ``throw_tick - 12`` fallback) for ``freeze_seconds`` (1.5s, 2.5s for
   3+-step recipes) in the main POV *before* the throw, so the order is
   freeze → throw → PiP. All holds go in with ONE ffmpeg pass (chained
   trim/loop/concat, CS2UtilArchive-style 1.5x crosshair hold + silence
   audio hold), last-frame-first so earlier indices stay valid. The
   round_offsets sidecar is expanded in memory AND rewritten on disk
   (with a ``freeze_windows`` record) so tick→frame mapping, batch
   boundaries, and the later voice-mix step all see the frozen timeline.
   Resume-safe: re-runs with identical specs skip; changed specs restore
   the pre-freeze video from ``../combined.mp4`` and redo.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from overlay._common import (
    _log,
    prefer_cs2util_scripts,
)

# Idle-keyboard fallback anchor: same 12-tick lead CS2UtilArchive uses for
# still throws (ANCHOR_BEFORE_THROW / LINEUP_AIM_FREEZE_ANCHOR_TICKS_BEFORE_THROW).
ANCHOR_LEAD_TICKS = 12
DEFAULT_FREEZE_SECONDS = 1.5
# Fallback release-spot tolerance for util types CS2UtilArchive doesn't list.
DEFAULT_SPOT_TOLERANCE = 96.0

_GRID_CACHE: dict[str, Any] = {}


def _cs2util(name: str):
    """Import a symbol from the sibling CS2UtilArchive checkout (or None)."""
    prefer_cs2util_scripts()
    try:
        if name == "cluster_throws_grid":
            from scripts.rank_utils import cluster_throws_grid
            return cluster_throws_grid
        if name == "release_tolerance":
            from scripts.select_top_utils import (
                release_spot_tolerance_for,
                RELEASE_SPOT_TOLERANCE_BY_UTIL_TYPE,
            )
            return release_spot_tolerance_for, RELEASE_SPOT_TOLERANCE_BY_UTIL_TYPE
        if name == "volumes":
            from scripts.render.volumes import (
                build_watershed_volumes,
                exit_distance,
                classify_throw,
            )
            return build_watershed_volumes, exit_distance, classify_throw
        if name == "los":
            from scripts.render.map_collision import closest_hit
            return closest_hit
        if name == "throw_type":
            from scripts.throw_type import (
                classify,
                freeze_anchor_tick,
                ANCHOR_BEFORE_THROW,
                FREEZE_SECONDS,
            )
            return classify, freeze_anchor_tick, ANCHOR_BEFORE_THROW, FREEZE_SECONDS
        if name == "freeze_consts":
            from scripts.render.ffmpeg_util import (
                FREEZE_CROSSHAIR_ZOOM,
                LINEUP_AIM_FREEZE_SECONDS,
            )
            return FREEZE_CROSSHAIR_ZOOM, LINEUP_AIM_FREEZE_SECONDS
    except Exception:
        return None
    return None


def lineup_tolerance_for(util_type: str) -> float:
    """Release-spot clustering tolerance for a util type (CS2UtilArchive's table)."""
    got = _cs2util("release_tolerance")
    if got is not None:
        release_spot_tolerance_for, _table = got
        try:
            return float(release_spot_tolerance_for(util_type, DEFAULT_SPOT_TOLERANCE))
        except Exception:
            pass
    return DEFAULT_SPOT_TOLERANCE


def _grid_cluster_ids(positions: list[tuple[float, float, float]], tolerance: float) -> list[int]:
    """Cell-cluster release positions (CS2UtilArchive's cluster_throws_grid, pure fallback)."""
    cluster = _cs2util("cluster_throws_grid")
    if cluster is not None:
        try:
            import numpy as np
            return [int(i) for i in cluster(np.asarray(positions, dtype=float), float(tolerance))]
        except Exception:
            pass
    step = tolerance if tolerance > 0 else 1.0
    cell_of: dict[tuple[int, int, int], int] = {}
    ids: list[int] = []
    for x, y, z in positions:
        cell = (math.floor(x / step), math.floor(y / step), math.floor(z / step))
        if cell not in cell_of:
            cell_of[cell] = len(cell_of)
        ids.append(cell_of[cell])
    return ids


def dedupe_throws_by_lineup(throws: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the earliest throw per (util_type, release-cell) lineup.

    Throws missing release coordinates pass through untouched (never
    dropped — dedupe must not destroy PiPs it can't place). Output is
    sorted by throw_tick. Idempotent.
    """
    by_type: dict[str, list[int]] = {}
    for i, t in enumerate(throws):
        if str(t.get("util_type", "")).lower() == "decoy":
            continue
        try:
            float(t["release_x"]), float(t["release_y"]), float(t["release_z"])
        except (KeyError, TypeError, ValueError):
            continue
        by_type.setdefault(str(t.get("util_type", "unknown")).lower(), []).append(i)

    drop: set[int] = set()
    for util_type, idxs in by_type.items():
        if len(idxs) < 2:
            continue
        tol = lineup_tolerance_for(util_type)
        pos = [
            (float(throws[i]["release_x"]), float(throws[i]["release_y"]), float(throws[i]["release_z"]))
            for i in idxs
        ]
        for cell, group in _cells(_grid_cluster_ids(pos, tol)).items():
            if len(group) < 2:
                continue
            members = sorted((idxs[g] for g in group), key=lambda i: int(throws[i].get("throw_tick", 0)))
            first = members[0]
            for dup in members[1:]:
                drop.add(dup)
            _log(f"  [lineup] {util_type} cell {cell}: "
                 f"{len(members)} throws -> keep t{throws[first].get('throw_tick')} "
                 f"(drop {[throws[d].get('throw_tick') for d in members[1:]]})")
    if drop:
        _log(f"  [lineup] deduped {len(drop)} repeat-lineup throws "
             f"({len(throws)} -> {len(throws) - len(drop)} PiPs)")
    kept = [t for i, t in enumerate(throws) if i not in drop]
    return sorted(kept, key=lambda t: int(t.get("throw_tick", 0)))


def _cells(ids: list[int]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for pos, cid in enumerate(ids):
        out.setdefault(cid, []).append(pos)
    return out


def select_window_throws(
    throws: list[dict[str, Any]],
    round_tick_ranges: dict[int, tuple[int, int]] | None,
) -> list[dict[str, Any]]:
    """Keep throws inside the recorded CSDM play windows.

    Dedupe must run on this subset: an earliest instance lost to the
    freeze/death cut must not shadow a watchable repeat of the same lineup.
    """
    if not round_tick_ranges:
        return list(throws)
    from overlay.overlay_utilcams import _play_window_for_throw
    return [
        t for t in throws
        if _play_window_for_throw(int(t.get("throw_tick", -1)), round_tick_ranges) is not None
    ]


# -- Straightforwardness wrapper -------------------------------------------


def _volume_grid(map_name: str):
    if map_name in _GRID_CACHE:
        return _GRID_CACHE[map_name]
    got = _cs2util("volumes")
    if got is None:
        return None
    build_watershed_volumes, _exit, _cls = got
    try:
        grid = build_watershed_volumes(map_name)
    except Exception as e:
        _log(f"  [freeze] no volume grid for {map_name} ({e}) — all throws straightforward")
        return None
    _GRID_CACHE[map_name] = grid
    return grid


def classify_throws_straightforward(
    throws: list[dict[str, Any]],
    *,
    data_dir: Path | None,
    map_name: str,
) -> dict[str, bool]:
    """Map throw_id -> True (straightforward toss, no freeze needed).

    Reference assembly mirrors CS2UtilArchive's temp/classify_nuke_map.py:
    trajectory xyz + eye=(throw_x,y,z) -> exit_distance on the watershed
    grid, los via closest_hit eye->detonate, rise = peak_z - eye_z.
    ANY failure (no trajectories, no mesh, short track) fails SAFE to True.
    """
    out: dict[str, bool] = {str(t.get("throw_id", "")): True for t in throws}
    got = _cs2util("volumes")
    if got is None:
        _log("  [freeze] CS2Util volumes unavailable — all throws straightforward")
        return out
    _build, exit_distance, classify_throw = got
    closest_hit = _cs2util("los")

    traj_by_id: dict[str, Any] = {}
    if data_dir is not None:
        tp = Path(data_dir) / "trajectories.parquet"
        if tp.is_file():
            try:
                import pandas as pd
                df = pd.read_parquet(tp)
                for tid, sub in df.groupby("throw_id"):
                    traj_by_id[str(tid)] = sub.sort_values("tick")
            except Exception as e:
                _log(f"  [freeze] trajectories unreadable ({e}) — all straightforward")
                return out
    if not traj_by_id:
        return out

    grid = _volume_grid(map_name)
    if grid is None:
        return out

    import numpy as np
    for t in throws:
        tid = str(t.get("throw_id", ""))
        try:
            sub = traj_by_id.get(tid)
            if sub is None or len(sub) < 4:
                continue
            xyz = sub[["x", "y", "z"]].to_numpy(float)
            eye = (float(t["throw_x"]), float(t["throw_y"]), float(t["throw_z"]))
            det = (float(t["detonate_x"]), float(t["detonate_y"]), float(t["detonate_z"]))
            dv = (det[0] - eye[0], det[1] - eye[1], det[2] - eye[2])
            dist = math.sqrt(dv[0] ** 2 + dv[1] ** 2 + dv[2] ** 2)
            if closest_hit is not None and dist > 1e-6:
                los_open = closest_hit(eye, dv, dist, map_name=map_name) is None
            else:
                # No mesh to test against, or eye == detonate (drop at feet):
                # sightline is trivially open -> straightforward, no freeze.
                los_open = True
            ex, rng, ev, lv = exit_distance(grid, np.asarray(xyz, dtype=float), eye)
            rise = float(t.get("peak_z", eye[2])) - eye[2]
            straight = bool(classify_throw(
                los_open=bool(los_open), same_volume=(ev == lv),
                exit_dist=float(ex), total_range=float(rng), rise=float(rise),
            ))
            out[tid] = straight
            if not straight:
                _log(f"  [freeze] lineup {t.get('util_type')} t{t.get('throw_tick')} "
                     f"(los_open={int(bool(los_open))} same_vol={int(ev == lv)} "
                     f"exit={ex:.0f} rng={rng:.0f} rise={rise:.0f})")
        except Exception:
            continue
    n_lineups = sum(1 for v in out.values() if not v)
    _log(f"  [freeze] {n_lineups} non-straightforward of {len(out)} throws")
    return out


# -- Anchor / hold specs ----------------------------------------------------


def freeze_specs_for_throws(
    throws: list[dict[str, Any]],
    straightforward: dict[str, bool],
    *,
    data_dir: Path | None,
) -> list[dict[str, Any]]:
    """Build (throw_tick, anchor_tick, hold_seconds) for non-straightforward throws.

    Motion-aware anchor via CS2UtilArchive's ``classify`` + ``freeze_anchor_tick``
    on input_overlay.parquet rows; falls back to ``throw_tick - 12`` / 1.5s when
    the overlay data or import is missing. Only throws with
    straightforward[throw_id] is False are returned.
    """
    specs: list[dict[str, Any]] = []
    overlay_by_id: dict[str, Any] = {}
    if data_dir is not None:
        ip = Path(data_dir) / "input_overlay.parquet"
        if ip.is_file():
            try:
                import pandas as pd
                df = pd.read_parquet(ip)
                for tid, sub in df.groupby("throw_id"):
                    overlay_by_id[str(tid)] = sub
            except Exception as e:
                _log(f"  [freeze] input_overlay unreadable ({e}) — using -12 tick anchors")

    tt = _cs2util("throw_type")
    classify, freeze_anchor_tick, anchor_lead, freeze_base = (tt + (None,) * 4)[:4] if tt else (None,) * 4
    lead = int(anchor_lead) if anchor_lead is not None else ANCHOR_LEAD_TICKS
    base_hold = float(freeze_base) if freeze_base is not None else DEFAULT_FREEZE_SECONDS

    for t in throws:
        tid = str(t.get("throw_id", ""))
        if straightforward.get(tid, True):
            continue
        throw_tick = int(t["throw_tick"])
        anchor = throw_tick - lead
        hold = base_hold
        typed = None
        g = overlay_by_id.get(tid)
        if g is not None and len(g) and classify is not None and freeze_anchor_tick is not None:
            try:
                typed = classify(g, throw_tick)
                anchor = int(freeze_anchor_tick(g, throw_tick, typed.motion))
                hold = float(typed.freeze_seconds)
            except Exception:
                typed = None
        specs.append({
            "throw_id": tid,
            "util_type": str(t.get("util_type", "unknown")),
            "throw_tick": throw_tick,
            "anchor_tick": max(0, anchor),
            "hold_seconds": hold,
            "typed": typed,
        })
    return specs


def compose_freeze_recipe_pngs(
    specs: list[dict[str, Any]],
    out_dir: Path,
    *,
    video_width: int,
    video_height: int,
) -> dict[str, Path]:
    """Compose CS2UtilArchive-style input recipe strips per throw_id.

    Uses the motion/click ThrowType already resolved for the freeze anchor
    (WASD holds, jump, crouch-release, mouse). Missing types or imports ->
    no PNG (freeze still applies, without the strip). Returns
    {throw_id: png_path}.
    """
    out: dict[str, Path] = {}
    try:
        prefer_cs2util_scripts()
        from scripts.render.overlay_assets import compose_freeze_recipe_png
        from scripts.throw_type import freeze_recipe
    except Exception as e:
        _log(f"  [freeze] recipe strips unavailable ({e})")
        return out
    for spec in specs:
        typed = spec.get("typed")
        if typed is None:
            continue
        try:
            slug = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(spec["throw_id"]))
            png = Path(out_dir) / f"_freeze_recipe_{slug}.png"
            compose_freeze_recipe_png(
                freeze_recipe(typed), png,
                video_height=video_height, video_width=video_width,
            )
            out[str(spec["throw_id"])] = png
        except Exception as e:
            _log(f"  [freeze] recipe strip skipped for t{spec.get('throw_tick')} ({e})")
    if out:
        _log(f"  [freeze] {len(out)} recipe strips composed")
    return out


# -- Single-pass freeze application -----------------------------------------


def _ffprobe_frames(path: Path) -> int | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    except Exception:
        return None


def _has_audio(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def apply_freezes_single_pass(
    video_path: Path,
    freezes: list[tuple[int, float]],
    *,
    fps: float,
    width: int,
    height: int,
    recipes: list[Path | None] | None = None,
    zoom: float | None = None,
) -> None:
    """Insert holds at (frame, hold_seconds) into video_path, ONE ffmpeg pass.

    Single-concat architecture: every segment (content runs + holds) is
    trimmed DIRECTLY from ``[0:v]`` in original-frame coordinates and joined
    by ONE trailing concat. Nothing consumes an intermediate, so chained
    concat/trim starvation (which silently dropped all but one hold) cannot
    happen. Ascending frame order; duplicate frames emit adjacent holds.

    Hold video is the CS2Util-style crosshair crop (default 1.5x); hold
    audio is silence. ``recipes[i]`` (optional) is a recipe-strip PNG baked
    onto hold ``i`` only (bottom-centre, CS2UtilArchive placement).
    Atomic: encodes to a PID-tagged ``.part`` then os.replace (breaks the
    pipeline's combined.mp4 hardlink safely — the source inode is never
    written; the PID tag keeps concurrent runs from sharing the part file).
    """
    import os as _os
    if not freezes:
        return
    if zoom is None:
        got = _cs2util("freeze_consts")
        zoom = float(got[0]) if got else 1.5
    frames = _ffprobe_frames(video_path)
    audio = _has_audio(video_path)
    fps_i = max(1, int(round(fps)))
    vw, vh = int(width), int(height)

    # Clamp into range; ascending (duplicates kept -> adjacent holds).
    # Items may be (frame, hold) pairs or (frame, hold, recipe_png|None)
    # triples; a parallel recipes list (same order as freezes) is folded in
    # BEFORE sorting so alignment survives.
    indexed: list[tuple[int, float, Path | None]] = []
    recs = list(recipes) if recipes else []
    triples: list[tuple[Any, Any, Any]] = []
    for i, item in enumerate(freezes):
        if len(item) == 3:
            triples.append((item[0], item[1], item[2]))
        else:
            triples.append((item[0], item[1], recs[i] if i < len(recs) else None))
    for idx, hold_s, r in sorted(triples, key=lambda p: p[0]):
        idx = max(0, int(idx))
        if frames is not None:
            idx = min(idx, max(0, frames - 1))
        if hold_s > 0:
            indexed.append((idx, float(hold_s), r if isinstance(r, Path) and r.is_file() else None))
    if not indexed:
        return

    parts: list[str] = []
    v_segs: list[str] = []
    a_segs: list[str] = []
    recipe_inputs: list[tuple[Path, float]] = []
    prev = 0
    total_hold_s = 0.0
    for n, (idx, hold_s, recipe) in enumerate(indexed):
        # Looped copies of the anchor frame. The hold REPLACES 1 content
        # frame with H looped copies, so net inserted = H - 1. For the
        # sidecar's hold_seconds to be exact, H - 1 must equal
        # round(hold_s * fps): loop count = round(hold_s * fps).
        hold_frames = max(2, int(round(hold_s * fps_i)) + 1)
        loop_n = hold_frames - 1
        # Content run before this hold (skipped when a previous hold ends
        # exactly here — e.g. duplicate freeze frames).
        if prev < idx:
            parts.append(f"[0:v]trim=start_frame={prev}:end_frame={idx},setpts=PTS-STARTPTS[fzv{n}h]")
            v_segs.append(f"[fzv{n}h]")
            if audio:
                parts.append(
                    f"[0:a]atrim=start={prev / fps_i:.6f}:end={idx / fps_i:.6f},"
                    f"asetpts=PTS-STARTPTS[fza{n}h]"
                )
                a_segs.append(f"[fza{n}h]")
        hold_src = (
            f"[0:v]trim=start_frame={idx}:end_frame={idx + 1},setpts=PTS-STARTPTS"
        )
        if zoom > 1.0:
            hold_src += f",scale={vw * zoom:.4f}:{vh * zoom:.4f},crop={vw}:{vh}"
        hold_src += f",loop=loop={loop_n}:size=1:start=0,setpts=N/FRAME_RATE/TB[fzv{n}o]"
        hold_label = f"fzv{n}o"
        if recipe is not None:
            r_idx = len(recipe_inputs) + 1  # input 0 is the video
            recipe_inputs.append((recipe, hold_s))
            hold_label = f"fzv{n}r"
            parts.append(hold_src)
            parts.append(
                f"[fzv{n}o][{r_idx}:v]overlay=(W-w)/2:H-h-80[{hold_label}]"
            )
        else:
            parts.append(hold_src)
        v_segs.append(f"[{hold_label}]")
        if audio:
            parts.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=end={hold_s:.6f},asetpts=PTS-STARTPTS[fza{n}o]"
            )
            a_segs.append(f"[fza{n}o]")
        total_hold_s += hold_s
        prev = idx + 1
    # Trailing content (skipped only when provably empty).
    if frames is None or prev < frames:
        parts.append(f"[0:v]trim=start_frame={prev},setpts=PTS-STARTPTS[fzvtail]")
        v_segs.append("[fzvtail]")
        if audio:
            parts.append(
                f"[0:a]atrim=start={prev / fps_i:.6f},asetpts=PTS-STARTPTS[fzatail]"
            )
            a_segs.append("[fzatail]")
    parts.append(f"{''.join(v_segs)}concat=n={len(v_segs)}:v=1:a=0[fzv]")
    if audio:
        parts.append(f"{''.join(a_segs)}concat=n={len(a_segs)}:v=0:a=1[fza]")
    fc = ";".join(parts)
    # Persist the graph next to the output for post-mortem debugging.
    try:
        video_path.with_name(video_path.name + ".freeze_fc.txt").write_text(fc, encoding="utf-8")
    except Exception:
        pass

    out = video_path.with_name(f"{video_path.name}.freeze.{_os.getpid()}.part")
    out.unlink(missing_ok=True)
    maps = ["-map", "[fzv]"]
    if audio:
        maps += ["-map", "[fza]"]
    else:
        maps += ["-an"]
    # Mezzanine (re-encoded again by the batch overlay below): CQ 8 / 200M
    # cap, same mezzanine profile as render/concat — never the final CQ 15.
    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    for rp, hold_s in recipe_inputs:
        # Finite loop: -t caps the still image at the hold length. An
        # unbounded -loop 1 still hangs the graph (overlay never sees EOF).
        cmd += ["-loop", "1", "-t", f"{hold_s:.6f}", "-i", str(rp)]
    cmd += [
        "-filter_complex", fc,
        *maps,
        "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "8",
        "-maxrate", "200M", "-bufsize", "400M",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
    ]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    # Bound the output duration exactly: -loop 1 recipe inputs are infinite
    # streams, and without a bound the encode never terminates. Total is
    # exact (content frames + hold frames at fps_i); the audio track runs
    # ~44ms long from AAC priming, so video is always the shortest stream.
    if frames is not None:
        total_frames = frames + sum(
            max(2, int(round(h * fps_i))) for _, h, _ in indexed
        )
        cmd += ["-t", f"{total_frames / fps_i:.6f}"]
    elif recipe_inputs:
        cmd += ["-shortest"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", str(out)]
    _log(f"  [freeze] {len(indexed)} holds, single pass "
         f"({sum(h for _, h, _ in indexed):.1f}s inserted)")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=21600)
    if r.returncode != 0 or not out.is_file():
        _log(f"[ERROR] freeze pre-pass failed: rc={r.returncode}")
        _log(f"  stderr: {(r.stderr or '')[-400:]}")
        out.unlink(missing_ok=True)
        sys.exit(1)
    # Structural check: output frames must equal input frames + hold frames.
    # The chained-concat design silently dropped holds here before; never again.
    if frames is not None:
        try:
            got = _ffprobe_frames(out)
            want = frames + sum(int(round(h * fps_i)) for _, h, _ in indexed)
            if got is not None and abs(got - want) > 1:
                _log(f"[ERROR] freeze frame check: got {got} frames, "
                     f"want {want} (in {frames} + holds) — refusing output")
                out.unlink(missing_ok=True)
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            _log(f"  [warn] freeze frame check skipped: {e}")
    _os.replace(out, video_path)


def split_straightforward(
    throws: list[dict[str, Any]],
    *,
    data_dir: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition throws into (lineups, straightforward).

    Lineups (non-straightforward) are the ONLY throws that get rendered,
    PiP'd, and frozen. Straightforward tosses are dropped entirely.
    Fail-safe: when classification cannot actually run (no trajectories,
    no volume grid), NOTHING is dropped — a PiP with a straightforward
    throw beats a missing-PiP validation failure.
    """
    if not throws:
        return [], []
    first = throws[0]
    map_name = str(first.get("map_name") or first.get("map") or "")
    trajs = Path(data_dir) / "trajectories.parquet" if data_dir is not None else None
    if trajs is None or not trajs.is_file() or not map_name \
            or _cs2util("volumes") is None or _volume_grid(map_name) is None:
        _log("  [lineup] straightforward filter unavailable "
             "(no trajectories/grid) — keeping all throws")
        return list(throws), []
    straight = classify_throws_straightforward(
        throws, data_dir=data_dir, map_name=map_name,
    )
    keep = [t for t in throws if not straight.get(str(t.get("throw_id", "")), True)]
    drop = [t for t in throws if straight.get(str(t.get("throw_id", "")), True)]
    if drop:
        _log(f"  [lineup] {len(drop)} straightforward throw(s) excluded "
             f"(no render/PiP/freeze): "
             + ", ".join(f"{t.get('util_type')} t{t.get('throw_tick')}" for t in drop[:8]))
    return keep, drop


# -- Sidecar expansion --------------------------------------------------------


def expand_offsets_for_freezes(
    round_offsets: dict[int, float],
    per_round_durations: dict[int, float],
    round_frame_ranges: dict[int, tuple[int, int]],
    freezes: list[tuple[int, float]],
    fps: float,
) -> tuple[dict[int, float], dict[int, float], list[dict[str, float]]]:
    """Shift round offsets/durations for inserted holds.

    Returns (new_offsets, new_durations, freeze_windows). Each window records
    the ORIGINAL anchor frame + inserted seconds so resume can compare specs
    without re-running the classifier. Pure function (unit-testable).
    """
    if not freezes:
        return dict(round_offsets), dict(per_round_durations), []
    by_round: dict[int, float] = {}
    frame_to_round: list[tuple[int, int, int]] = [
        (fs, fe, rn) for rn, (fs, fe) in round_frame_ranges.items()
    ]
    windows: list[dict[str, float]] = []
    for frame, hold_s in sorted(freezes, key=lambda p: p[0]):
        rn_hit = None
        for fs, fe, rn in frame_to_round:
            if fs <= frame <= fe:
                rn_hit = rn
                break
        if rn_hit is None:
            continue
        by_round[rn_hit] = by_round.get(rn_hit, 0.0) + hold_s
        windows.append({"frame": float(frame), "hold_seconds": float(hold_s),
                        "round": float(rn_hit)})
    if not by_round:
        return dict(round_offsets), dict(per_round_durations), []
    new_offsets = dict(round_offsets)
    new_durations = dict(per_round_durations)
    shift = 0.0
    for rn in sorted(new_offsets.keys()):
        new_offsets[rn] = new_offsets[rn] + shift
        add = by_round.get(rn, 0.0)
        if rn in new_durations:
            new_durations[rn] = new_durations[rn] + add
        shift += add
    _ = fps  # frame ranges are rebuilt downstream from the new offsets
    return new_offsets, new_durations, windows
