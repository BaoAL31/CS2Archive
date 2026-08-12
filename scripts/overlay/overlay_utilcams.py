"""Utility-cam (throw flight) rendering for the overlay pipeline.

CS2Archive-local logic that drives CS2UtilArchive's batched CSDM renderer.
Shared leaf symbols come from ``overlay._common``; reusable overlay
primitives (sprite gen, decode, paths) live in the CS2UtilArchive sibling.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from overlay._common import (
    _CS2UTIL_ROOT,
    _CS2UTIL_SCRIPTS,
    TICKRATE,
    _log,
    _probe_clip_duration_seconds,
)
from scripts.render.paths import clip_name_for_cameras, util_render_slug



def _cs2util_results_dir() -> Path | None:
    d = _CS2UTIL_ROOT / "results"
    return d if d.is_dir() else None


def _find_demo_data_dir(demo_path: Path) -> Path | None:
    """Find CS2UtilArchive data dir for this demo (where throws.parquet lives)."""
    results = _cs2util_results_dir()
    if results is None:
        return None
    exact = demo_path.stem
    broad = re.sub(r"-p\d+$", "", demo_path.stem, flags=re.IGNORECASE)

    # Search all project subdirs: results/*/data/demo=<name>/
    for project_dir in results.iterdir():
        if not project_dir.is_dir():
            continue
        data_dir = project_dir / "data"
        if not data_dir.is_dir():
            continue
        for d in data_dir.iterdir():
            if not d.is_dir() or not d.name.startswith("demo="):
                continue
            dn = d.name[len("demo="):]
            if dn == exact or broad in dn:
                return d
    return None


# -- Round tick ranges ---------------------------------------------------



@dataclass
class PipClip:
    clip_path: Path
    start_frame: int
    end_frame: int
    util_type: str
    pip_index: int = 0


def _load_player_throws(
    demo_path: Path,
    steam_id: str,
    round_start_tick: int = 0,
    round_end_tick: int = 0,
) -> list[dict[str, Any]] | None:
    """Load player's renderable throws from CS2UtilArchive throws.parquet.

    Filters to throws with flight_ticks > 0, optionally within round tick range.
    Returns None (sentinel) when CS2UtilArchive has NOT processed this demo
    (no data dir / no throws.parquet) — callers treat that as a hard failure
    because the utility-cam overlay cannot be produced. Returns [] only when
    the demo WAS analyzed but this player has no flight throws (legitimate).
    """
    data_dir = _find_demo_data_dir(demo_path)
    if data_dir is None:
        _log("  [throws] No CS2UtilArchive data dir found for this demo")
        return None

    throws_path = data_dir / "throws.parquet"
    if not throws_path.is_file():
        _log(f"  [throws] throws.parquet not found at {throws_path}")
        return None

    import pandas as pd
    df = pd.read_parquet(throws_path)
    sid = int(steam_id)
    player_df = df[
        (df["thrower_steamid"] == sid)
        & (df["flight_ticks"] > 0)
        & (df["is_renderable"] == True)  # noqa: E712  (renderer skips these too)
    ].copy()

    if round_start_tick > 0:
        end = round_end_tick if round_end_tick > 0 else round_start_tick + int(45 * 60 * TICKRATE)
        player_df = player_df[
            (player_df["throw_tick"] >= round_start_tick)
            & (player_df["throw_tick"] <= end)
        ]

    if player_df.empty:
        _log("  [throws] No throws with flight for this player")
        return []

    _log(f"  [throws] {len(player_df)} renderable throws")
    side_counts = player_df["thrower_side"].value_counts().to_dict()
    t_count = int(side_counts.get("T", 0))
    ct_count = int(side_counts.get("CT", 0))
    _log(f"  [throws] side breakdown: T={t_count} CT={ct_count}")
    if (t_count == 0 or ct_count == 0) and (t_count + ct_count) > 0:
        missing = "CT" if ct_count == 0 else "T"
        _log(f"  [throws] NOTE: zero throws on {missing} side — data observation, not pipeline bug")
    return [dict(row) for _, row in player_df.iterrows()]


def _ensure_cs2util_data(demo_path: Path) -> None:
    """Auto-extract CS2UtilArchive data if missing for this demo."""
    if _find_demo_data_dir(demo_path) is not None:
        return
    _log(f"  [extract] CS2UtilArchive data missing for {demo_path.stem} — auto-extracting...")

    # Determine demo_id same way CS2UtilArchive does
    from scripts.demo_ids import default_demo_id_from_path
    demo_id = default_demo_id_from_path(demo_path)

    # Ensure .dem is in CS2UtilArchive's demos/extracted/ directory
    match_dir = demo_path.parent.name
    extracted_demo_dir = _CS2UTIL_ROOT / "demos" / "extracted" / match_dir
    extracted_demo_path = extracted_demo_dir / demo_path.name
    if not extracted_demo_path.is_file():
        extracted_demo_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(demo_path, extracted_demo_path)
        _log(f"  [extract] Copied {demo_path.name} -> {extracted_demo_path}")

    # Add CS2UtilArchive scripts to sys.path for imports
    cs2_scripts = str(_CS2UTIL_SCRIPTS)
    if cs2_scripts not in sys.path:
        sys.path.insert(0, cs2_scripts)

    from scripts.extract_utils import process_demo
    summary = process_demo(
        str(extracted_demo_path),
        output_dir=str(_CS2UTIL_ROOT / "results" / "auto_extracted" / "data"),
        demo_id=demo_id,
    )
    _log(f"  [extract] Done: {summary['n_throws']} throws, {summary['n_trajectories']} trajectory points")


def _build_round_frame_ranges(
    round_offsets: dict[int, float],
    round_tick_ranges: dict[int, tuple[int, int]],
    fps: float,
    total_frames: int,
) -> dict[int, tuple[int, int]]:
    """Build per-round frame ranges from round_offsets sidecar.

    Returns {round_num: (start_frame, end_frame)}.
    """
    sorted_rounds = sorted(round_offsets.keys())
    result: dict[int, tuple[int, int]] = {}
    for i, rn in enumerate(sorted_rounds):
        start_frame = int(round_offsets[rn] * fps)
        if i + 1 < len(sorted_rounds):
            end_frame = int(round_offsets[sorted_rounds[i + 1]] * fps) - 1
        else:
            end_frame = total_frames - 1
        result[rn] = (start_frame, end_frame)
    return result


def _concat_two_clips(a: Path, b: Path, out: Path) -> None:
    """Concat two mp4 clips (same codec/params) into one temp PiP clip."""
    import tempfile
    fd, listf = tempfile.mkstemp(suffix=".txt", prefix="concat_")
    try:
        Path(listf).write_text(
            f"file '{a.resolve()}'\nfile '{b.resolve()}'\n", encoding="utf-8"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
             "-c", "copy", str(out.resolve())],
            check=True, capture_output=True,
        )
    finally:
        try:
            Path(listf).unlink()
        except OSError:
            pass


def _rm_empty_dir(d: Path) -> None:
    """Remove *d* if it exists and contains no files (skips subdirs)."""
    if d.is_dir() and not any(d.iterdir()):
        try:
            d.rmdir()
        except OSError:
            pass


def _util_slug_for_throw(throw: dict, demo_path: Path) -> tuple[str, str, str]:
    """Return (util_id, util_slug, demo_id) for a throw row.

    util_id = ``<map>:<util_type>:<side>:<land_x>_<land_y>_<land_z>`` (landing
    position, no match id) — matches CS2UtilArchive's render_utils folder
    architecture. util_slug = util_render_slug(util_id).
    """
    map_name = str(throw.get("map") or throw.get("map_name") or demo_path.stem)
    util_type = str(throw.get("util_type", "unknown")).lower()
    side = str(throw.get("thrower_side", "T") or "T").upper()
    land_x = int(round(float(throw.get("land_x", 0) or 0)))
    land_y = int(round(float(throw.get("land_y", 0) or 0)))
    land_z = int(round(float(throw.get("land_z", 0) or 0)))
    util_id = f"{map_name}:{util_type}:{side}:{land_x}_{land_y}_{land_z}"
    util_slug = util_render_slug(util_id)
    demo_id = re.sub(r"^\d{6,}-", "", str(throw.get("demo_id", demo_path.stem)))
    return util_id, util_slug, demo_id


def _run_batch_util_cams_subprocess(
    demo_path: Path,
    steam_id: str,
    data_dir: Path,
    util_cams_root: Path,
    chunk_size: int = 0,
    demo_data_dir_name: str | None = None,
) -> int:
    """Shell out to scripts/overlay/render_util_cams.py for util_cam prep + render.

    Bypasses the inline run_csdm loop (Bug A: random POV instead of chase cam
    when the inject thread races csdm's actions-file write). render_util_cams.py
    handles BOTH prep (filter throws.parquet by steamid, create util_cam dirs
    + _throw_poses.json) and render (call CS2UtilArchive's render_spot_batch
    in one CS2 launch per chunk of N spots). Idempotent — re-runs are no-ops
    for already-rendered clips.
    """
    import subprocess
    script_path = Path(__file__).resolve().parent / "render_util_cams.py"
    # Extract demo_id from the per-demo data dir name. Caller passes the
    # leaf explicitly because the parent (data_dir) doesn't start with "demo=".
    # Leaf: "demo=2395002-furia-vs-falcons-m2-anubis" → "2395002-furia-vs-falcons-m2-anubis".
    demo_id = None
    if demo_data_dir_name and demo_data_dir_name.startswith("demo="):
        demo_id = demo_data_dir_name[len("demo="):]
    elif data_dir and data_dir.name.startswith("demo="):
        demo_id = data_dir.name[len("demo="):]
    cmd = [
        sys.executable, str(script_path),
        "--util-cams-root", str(util_cams_root.resolve()),
        "--data-dir", str(data_dir.resolve()),
        "--steamid", str(steam_id),
        "--chunk-size", str(chunk_size),
    ]
    if demo_id:
        cmd += ["--demo-id", demo_id]
    # Derive demos_dir from CS2UtilArchive project root
    demos_dir = _CS2UTIL_ROOT / "demos" / "extracted"
    cmd += ["--demos-dir", str(demos_dir.resolve())]
    _log(f"  [flight] CMD: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=str(util_cams_root.parent.parent.parent),
            check=False,
            timeout=3600,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            _log(f"  [flight] stdout: {result.stdout[-2000:]}")
        if result.stderr:
            _log(f"  [flight] stderr: {result.stderr[-2000:]}")
        return result.returncode
    except subprocess.TimeoutExpired:
        _log(f"  [flight] TIMEOUT: render_util_cams.py exceeded 3600s — "
             f"CS2 likely failed to launch (secondary drive?). "
             f"Open CS2 manually and re-run.")
        return 124
    except Exception as exc:
        _log(f"  [flight] render_util_cams.py subprocess failed: {exc}")
        return 1


def _throw_id_file_slug(throw_id: str) -> str:
    """Filesystem slug for a throw_id (match-id stripped, colons → underscores).

    ``2395001-foo:e246:s2`` → ``foo_e246_s2``. Must include segment (``_sN``)
    so ``e246:s2`` never matches ``e246_s4`` filenames.
    """
    stripped = re.sub(r"^\d{6,}-", "", str(throw_id))
    return stripped.replace(":", "_")


def _scan_utility_cams_clips(video_path: Path) -> dict[str, Path]:
    """Scan utility_cams for pre-rendered clips (orbit + victims + flight).

    Uses _throw_poses.json files to map throw_id -> mp4 clip. Matches by the
    full throw slug (``…_eN_sM``), never by bare entity id — substring match
    on ``e246`` alone wrongly picks ``e246_s4`` for throw ``e246:s2``.
    """
    pre_rendered: dict[str, Path] = {}
    p = video_path.parent
    util_cams = None
    for _ in range(5):
        cand = p / "utility_cams"
        if cand.is_dir():
            util_cams = cand
            break
        p = p.parent
    if not util_cams or not util_cams.is_dir():
        return {}

    for poses_f in util_cams.rglob("_throw_poses.json"):
        try:
            poses = json.loads(poses_f.read_text())
        except Exception:
            continue
        throw_map = poses.get("_throws", {})
        if not throw_map:
            continue
        mp4s = sorted(poses_f.parent.glob("*.mp4"))
        if not mp4s:
            continue
        for tid in throw_map:
            slug = _throw_id_file_slug(tid)
            if not slug:
                continue
            # Prefer flight clip; exact slug match (segment-inclusive).
            matching = [
                m for m in mp4s
                if slug in m.stem and m.stem.endswith(slug)
            ]
            if not matching:
                matching = [m for m in mp4s if f"_{slug}" in m.stem or m.stem.endswith(slug)]
            flight = [m for m in matching if m.name.startswith("flight") and "detonate" not in m.name]
            pick = flight[0] if flight else (matching[0] if matching else None)
            if pick is not None:
                pre_rendered[tid] = pick
    return pre_rendered


def _map_throw_tick_to_frame(
    throw_tick: int,
    round_tick_ranges: dict[int, tuple[int, int]],
    round_frame_ranges: dict[int, tuple[int, int]],
) -> int | None:
    """Map a demo throw_tick to a video frame using CSDM play tick spans.

    Do NOT use throws.parquet ``round_num`` — it is often off-by-one vs the
    CSDM sequence tick windows in the sidecar, which places PiPs in the wrong
    round (e.g. a round-5 fire appearing in early-round video).

    Returns None when the throw falls outside every recorded play window
    (buy/freeze cut or death cut) — caller should skip the PiP.
    """
    for rn, (rs, re) in round_tick_ranges.items():
        if rn not in round_frame_ranges:
            continue
        if rs <= throw_tick <= re:
            fs, fe = round_frame_ranges[rn]
            rf = (re - rs) or 1
            return int(fs + (throw_tick - rs) / rf * (fe - fs))
    # Within ~2s before a play window (still in freeze that CSDM trimmed):
    # clamp to that round's first frame rather than dropping the util.
    for rn, (rs, re) in sorted(round_tick_ranges.items()):
        if rn not in round_frame_ranges:
            continue
        if 0 < (rs - throw_tick) <= int(2 * TICKRATE):
            fs, _ = round_frame_ranges[rn]
            return fs
    return None


def _render_throw_flight_clips(
    demo_path: Path,
    steam_id: str,
    fps: float,
    frame_count: int,
    output_dir: Path,
    video_path: Path | None = None,
    round_offsets: dict[int, float] | None = None,
    round_tick_ranges: dict[int, tuple[int, int]] | None = None,
    total_duration_seconds: float = 0.0,
    util_cams_root: Path | None = None,
) -> list[PipClip]:
    """Render CSDM flight clips for each player throw.

    Shells out to scripts/batch_util_cams.py (Batched CSDM — one CS2 launch
    per chunk of N spots, spec_goto precomputed). Fix for Bug A: inline
    run_csdm loop races csdm's actions-file write → random POV instead of
    chase cam. See scripts/batch_util_cams.py for batching details.
    Outputs 1920x1080 clips to <util_cams_root>/unnamed/<throw_id_slug>/ (match-id
    prefix stripped, matching CS2UtilArchive's render_utils folder architecture).
    Returns PipClip metadata sorted by start_frame.
    """
    # Determine first round tick for filtering
    first_round_tick = 0
    last_round_tick = 0
    if round_tick_ranges and round_offsets:
        first_round = min(round_offsets.keys())
        last_round = max(round_offsets.keys())
        rs, _ = round_tick_ranges.get(first_round, (0, 0))
        _, re = round_tick_ranges.get(last_round, (0, 0))
        first_round_tick = rs
        last_round_tick = re

    throws = _load_player_throws(demo_path, steam_id, first_round_tick, last_round_tick)
    if throws is None:
        _log("[extract] CS2UtilArchive data missing — auto-extracting...")
        _ensure_cs2util_data(demo_path)
        throws = _load_player_throws(demo_path, steam_id, first_round_tick, last_round_tick)
        if throws is None:
            _log("[ERROR] CS2UtilArchive data still missing after auto-extract")
            sys.exit(1)
    if not throws:
        return []

    # Load trajectories once (per-throw chase-cam injection needs them).
    # Bug A fix: without trajectories + throw_pose + run_csdm inject thread,
    # csdm free-cams a random POV instead of chasing the grenade.
    data_dir = _find_demo_data_dir(demo_path)
    traj_by_throw: dict[str, Any] = {}
    if data_dir is not None:
        traj_path = data_dir / "trajectories.parquet"
        if traj_path.is_file():
            import pandas as _pd
            _traj_df = _pd.read_parquet(traj_path)
            for tid, sub in _traj_df.groupby("throw_id"):
                traj_by_throw[str(tid)] = sub.sort_values("tick").copy()
            _log(f"  [flight] Loaded {len(traj_by_throw)} trajectories")
        else:
            _log(f"  [flight] WARN: {traj_path.name} missing — flight cams will be skipped")
    else:
        _log(f"  [flight] WARN: no CS2UtilArchive data dir — flight cams will be skipped")

    # Resolve utility_cams directory. Explicit --util-cams-root wins (used by
    # pipeline in dual-upload mode to point at the persistent render cache
    # under renders/, not a freshly-created dir under youtube/).
    if util_cams_root is not None:
        util_cams_root = Path(util_cams_root)
        util_cams_root.mkdir(parents=True, exist_ok=True)
    else:
        # Walk up from video looking for an existing utility_cams/ cache.
        video_dir = video_path.parent if video_path else output_dir
        resolved: Path | None = None
        p = video_dir
        for _ in range(5):
            cand = p / "utility_cams"
            if cand.is_dir():
                resolved = cand
                break
            p = p.parent
        if resolved is None:
            resolved = video_dir / "utility_cams"
        resolved.mkdir(parents=True, exist_ok=True)
        util_cams_root = resolved

    # Build per-round frame ranges from round_offsets
    round_frame_ranges = {}
    if round_offsets and round_tick_ranges:
        round_frame_ranges = _build_round_frame_ranges(
            round_offsets, round_tick_ranges, fps, frame_count,
        )
        _log(f"  [flight] {len(round_frame_ranges)} round frame ranges")

    # Scan pre-rendered clips from utility_cams (_throw_poses.json -> mp4)
    pre_rendered: dict[str, Path] = {} if video_path is None else _scan_utility_cams_clips(video_path)
    if pre_rendered:
        _log(f"  [flight] Found {len(pre_rendered)} pre-rendered clips in utility_cams")

    # Determine if any throw still needs rendering (skip subprocess if all done).
    # A throw is "covered" if either:
    #   (a) its throw_id is in pre_rendered, OR
    #   (b) its util_cam dir contains a shared throw_flight_*.mp4 ≥1MB
    #       (a different throw at the same release position was batched)
    needs_render = False
    for throw in throws:
        tid = str(throw.get("throw_id", ""))
        # util_id-keyed dir: unnamed/<util_id_slug>/ (no match id), matching
        # CS2UtilArchive's render_utils folder architecture. Multiple throws
        # at the same landing spot share one dir.
        _, uid_slug, _ = _util_slug_for_throw(throw, demo_path)
        render_dir_check = util_cams_root / "unnamed" / uid_slug
        util_type = str(throw.get("util_type", "unknown")).lower()
        # Decoys / non-renderable throws have no flight trajectory, so the
        # batch render can't produce a clip for them — never flag as
        # needs_render (and never error on their missing clip downstream).
        if util_type == "decoy" or not bool(throw.get("is_renderable", True)):
            continue
        # Smoke/fire/molotov MUST have BOTH flight + detonate clips rendered.
        # If detonate is missing we flag needs_render so the batch subprocess
        # re-renders it (never silently continue). flash/he only need flight.
        if util_type in ("smoke", "fire", "molotov", "incendiary"):
            required = ["flight", "detonate"]
        else:
            required = ["flight"]
        clips_ok = all(
            (render_dir_check / f"{clip_name_for_cameras(c, tid)}.mp4").is_file()
            and (render_dir_check / f"{clip_name_for_cameras(c, tid)}.mp4").stat().st_size > 100_000
            for c in required
        )
        if tid in pre_rendered or clips_ok:
            continue
        needs_render = True
        break

    if needs_render and data_dir is not None:
        _log(f"  [flight] Subprocess: batch_util_cams.py (batched, one CS2 launch per chunk)")
        # data_dir is the per-demo dir (e.g. demo=2395002-furia-vs-falcons-m2-anubis).
        # batch_util_cams.py expects the PARENT (containing demo=* subdirs).
        # Pass both: parent to the subprocess, leaf to extract --demo-id.
        data_dir_parent = data_dir.parent
        rc = _run_batch_util_cams_subprocess(
            demo_path=demo_path,
            steam_id=steam_id,
            data_dir=data_dir_parent,
            util_cams_root=util_cams_root,
            demo_data_dir_name=data_dir.name,
        )
        if rc != 0:
            _log(f"  [flight] batch render FAILED (rc={rc}) — aborting flight clips")
            return []
        # Re-scan after batch render to pick up newly written mp4s + _throw_poses.json
        pre_rendered = _scan_utility_cams_clips(video_path) if video_path else {}
        if pre_rendered:
            _log(f"  [flight] After batch: {len(pre_rendered)} clips now available")
    elif needs_render and data_dir is None:
        _log(f"  [flight] WARN: no CS2UtilArchive data dir — cannot batch-render")

    clips: list[PipClip] = []
    for idx, throw in enumerate(throws):
        throw_tick = int(throw["throw_tick"])
        util_type = str(throw.get("util_type", "unknown")).lower()
        throw_round = int(throw.get("round_num", 0))

        # Decoys and other non-renderable throws never produce a flight clip
        # (no trajectory / flagged not renderable) and must be SKIPPED, not
        # errored. We only fail loudly when a RENDERABLE throw's expected clip
        # is missing — that is a genuine render gap that needs fixing. This
        # preserves the prior skip behavior for decoys; the hard error is
        # reserved for real missing-clip cases.
        if util_type == "decoy" or not bool(throw.get("is_renderable", True)):
            continue

        # Frame START from throw_tick vs CSDM play tick windows — NOT parquet
        # round_num (often off-by-one vs sequence files → PiP in wrong round).
        round_end_frame: int | None = None
        start_frame: int | None = None
        if round_tick_ranges and round_frame_ranges:
            start_frame = _map_throw_tick_to_frame(
                throw_tick, round_tick_ranges, round_frame_ranges,
            )
            if start_frame is None:
                # Outside every play window (freeze/death cut) — skip rather
                # than misplace into an early round via max(0, …).
                _log(f"  [flight] SKIP {util_type} throw {idx} t{throw_tick}: "
                     f"outside CSDM play tick windows (would mis-time PiP)")
                continue
            for rn, (fs, fe) in round_frame_ranges.items():
                if fs <= start_frame <= fe:
                    round_end_frame = fe
                    break
        elif first_round_tick > 0:
            start_frame = int((throw_tick - first_round_tick) * fps / TICKRATE)
        else:
            start_frame = int(throw_tick * fps / TICKRATE)

        start_frame = max(0, int(start_frame))

        throw_id = str(throw.get("throw_id", ""))
        # util_id-keyed dir (no match id); clip name via clip_name_for_cameras,
        # matching what render_util_cams.py / render_spot_batch wrote.
        _, uid_slug, _ = _util_slug_for_throw(throw, demo_path)
        render_dir = util_cams_root / "unnamed" / uid_slug

        def _pick(preferred: Path, want_detonate: bool) -> Path:
            """Return the EXACT preferred clip and nothing else.

            For smokes/molotov that is the COMBINED ``flight_detonate`` clip
            (throw arc + detonation in one file); for other util it is the
            plain ``flight`` clip. There is NO directory-scan fallback: if the
            correct clip is missing the caller must (re-)render it. We never
            substitute a wrong clip (e.g. a standalone ``detonate`` showing a
            static smoke with no throw) — instead we fail loudly so the missing
            render gets fixed rather than silently shipping bad output.
            """
            if preferred.is_file() and preferred.stat().st_size > 100_000:
                return preferred
            if throw_id in pre_rendered and pre_rendered[throw_id].is_file() \
                    and pre_rendered[throw_id].stat().st_size > 100_000:
                return pre_rendered[throw_id]
            _log(f"  [flight] ERROR: expected clip missing for "
                 f"{render_dir.name}: {preferred.name}")
            sys.exit(1)

        # Smoke/fire/molotov: CS2UtilArchive renders SEPARATE ``flight``
        # (throw arc) and ``detonate`` (settled smoke) clips — there is NO
        # combined ``flight_detonate`` file. The PiP must show the throw arc
        # (flight) then the detonation. We build a temp combined clip =
        # flight + detonate (concat) so the viewer sees the grenade fly then
        # settle. BOTH clips are REQUIRED — if either is missing we error hard
        # (no fallback). Shipping a partial clip is the old bug.
        if util_type in ("smoke", "fire", "molotov", "incendiary"):
            flight_clip = render_dir / f"{clip_name_for_cameras('flight', throw_id)}.mp4"
            detonate_clip = render_dir / f"{clip_name_for_cameras('detonate', throw_id)}.mp4"
            if flight_clip.is_file() and flight_clip.stat().st_size > 100_000 \
                    and detonate_clip.is_file() and detonate_clip.stat().st_size > 100_000:
                # Concat flight -> detonate into a temp combined clip.
                combined = render_dir / f"flight_detonate_{throw_id.replace(':', '_')}.mp4"
                if not (combined.is_file() and combined.stat().st_size > 100_000):
                    _concat_two_clips(flight_clip, detonate_clip, combined)
                clip_path = combined
            else:
                _log(f"  [flight] ERROR: missing flight/detonate clip for {util_type} "
                     f"throw {idx} (t{throw_tick}) flight={flight_clip.name} "
                     f"detonate={detonate_clip.name} — must render both first")
                sys.exit(1)
        else:
            # flash/he/decoy: plain flight clip.
            clip_path = _pick(render_dir / f"{clip_name_for_cameras('flight', throw_id)}.mp4",
                              want_detonate=False)

        if not clip_path.is_file() or clip_path.stat().st_size < 100_000:
            _log(f"  [flight] ERROR: no usable clip for {util_type} throw {idx} "
                 f"at {clip_path.name} (t{throw_tick}) — must render it first")
            sys.exit(1)

        # Window: actual rendered clip length, anchored at the throw frame,
        # ... clamped to the throwing round's frame end so a smoke thrown late
        # in a round cannot bleed into a later round's batch (which would
        # otherwise replay the clip at frame 0 of that batch = next-round start).
        clip_dur = _probe_clip_duration_seconds(clip_path)
        dur_frames = max(1, int(round(clip_dur * fps))) if clip_dur > 0 else 1
        end_frame = start_frame + dur_frames
        if round_end_frame is not None and start_frame < round_end_frame:
            end_frame = min(end_frame, round_end_frame)
        end_frame = min(end_frame, frame_count - 1)
        if start_frame >= end_frame:
            _log(f"  [flight] SKIP {util_type} throw {idx}: start_frame={start_frame} >= end_frame={end_frame} (clip_dur={clip_dur})")
            continue

        clips.append(PipClip(
            clip_path=clip_path,
            start_frame=start_frame,
            end_frame=end_frame,
            util_type=util_type,
        ))

    _log(f"  [flight] {len(clips)} throw clips rendered")
    return sorted(clips, key=lambda c: c.start_frame)


# -- Composite overlay ---------------------------------------------------


