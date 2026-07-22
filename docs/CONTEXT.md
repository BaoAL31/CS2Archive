# Overlay POV Resumability — Full Context

## 1. overlay_pov.py Structure

File: `scripts/overlay/overlay_pov.py` (single `run_overlay()` entry point)

### Flow (top-down)

1. **`run_overlay(video_path, demo_path, steam_id, round_num=None)`** — main entry
   - Probes video: `_probe_video_info()` → (w, h, fps, frame_count)
   - Loads `combined.round_offsets.json` sidecar (written by `concat_rounds.py`)
   - Loads round tick ranges: `_load_round_tick_ranges()` (from parquet or demoparser2)
   - Computes POV play tick ranges: `_load_pov_play_tick_ranges()` (adjusted by CSDM_TICK_MARGIN=128)
   - **Step 1: Keyboard states** — `_extract_keyboard_states()` (demoparser2, 0.5-2s)
   - **Step 2: Generate sprites** — `generate_key_assets()` → 18 PNGs (deterministic, <1s)
   - **Step 3: Render flight clips** — `_render_throw_flight_clips()` **(expensive: 1-2 min per throw)**
   - **Step 4: Composite** — ffmpeg filter_complex (2-pass if both keyboard + flight clips)
   - Writes `video.overlay.mp4` sidecar (NEVER modifies original video)
   - Cleans up temp work dir

### Functions & Side Effects

| Function | Side Effects | Idempotent? | Time |
|---|---|---|---|
| `_probe_video_info()` | None | Yes | <1s |
| `_load_round_tick_ranges()` | None | Yes | 1-5s (parquet faster) |
| `_load_pov_play_tick_ranges()` | None | Yes | 5-10s (demoparser2) |
| `_extract_keyboard_states()` | None | Yes | 0.5-2s |
| `generate_key_assets()` | Writes 18 PNGs to `{work_dir}/sprites/` | Deterministic | <1s |
| `_render_throw_flight_clips()` | **Renders CSDM flight clips via HLAE** | **No (expensive re-render)** | 1-2 min per throw |
| `_build_pip_chain()` | None | Yes | <1s |
| `_ffmpeg_encode()` | Writes output video | Yes (overwrite) | 5-15 min for full match |
| `_scan_utility_cams_clips()` | None (reads only) | Yes | <1s |

### File Outputs

- **`video.overlay.mp4`** — always written as sidecar next to input video
- **`{work_dir}/sprites/*.png`** — 18 key cap sprite PNGs (temp dir, deleted in `finally`)
- **`{util_cams_root}/unnamed/{util_slug}/{demo_id}/flight_{throw_id_slug}.mp4`** — per-throw flight clips (PERSISTENT cache)
- **`{util_cams_root}/unnamed/{util_slug}/{demo_id}/_throw_poses.json`** — metadata for pre-rendered clip scan

### Key: No existing resumability

The overlay script has NO internal resumability. On each run:
- Keyboard extraction runs every time (cheap, fine)
- Sprite generation runs every time (cheap, fine)
- Flight clip rendering checks if clip file exists >100KB → skips if present
- ffmpeg composite always re-encodes from scratch (the expensive part per-run)

The flight clip check `_scan_utility_cams_clips()` provides *partial* resume for flight renders since clips are cached in `utility_cams/`. But there's NO pipeline-level resume for the overlay step — if the pipeline crashes mid-way through flight rendering, **all prior renders are lost on next pipeline run**.

## 2. Pipeline State Management

### State file schema: `.pipeline/{run_id}.json`

```json
{
  "step": 7,
  "data": {
    "steam_id": "76561198041683378",
    "render_dir": "D:\\...\\pov-{demo}_{player}",
    "youtube_dir": "D:\\...\\youtube\\{match_id}_{demo}_{player}_{map}",
    "ratings_path": "demos\\analysis\\..._ratings.json",
    "avatar_path": "demos\\avatars\\{player}.png",
    "youtube_id": "abc123",           // after upload
    "overlay_youtube_id": "abc124",    // dual-upload variant (after upload)
    "dual_upload": true                // if --dual-upload was passed
  }
}
```

### Resume pattern (pipeline.py)

- `load_state(run_id)` — loads JSON, returns `{"step": 1, "data": {}}` if missing
- `save_state(run_id, state)` — writes after each step completes
- Each step method runs `getattr(self, f"step_{step_name}")()` then sets `state["step"] = step_num + 1`
- `__init__` computes `self.run_id = f"{match_id}_{dem_stem}_{player}_{map}"` 
- `--step N` flag skips completed steps

### Step 4 overlay: currently NOT resumable within step

Current `step_overlay()`:
```python
def step_overlay(self):
    # Determine target_dir (dual-upload variant or raw)
    # Run overlay_pov.py as subprocess
    # Check for video.overlay.mp4 sidecar
    # If dual-upload: replace video.mp4 with overlay version
    # If raw: leave sidecar orphaned
```

If overlay_pov.py crashes mid-flight-render, the pipeline state shows `"step": 4`. **On retry, the step runs overlay_pov.py from scratch**, re-rendering all flight clips (unless they exist in utility_cams cache). No state tracking of flight renders within the overlay step itself.

### Steps after overlay (5-8) that assume video.mp4 exists

- **Step 5 (outro)**: `_append_outro(youtube_dir)` — appends 5s outro to `video.mp4`, writes temp + concat
- **Step 6 (thumbnail)**: `_generate_thumbnail()` — generates JPG, writes upload_meta.json
- **Step 7 (upload)**: `_upload_variant()` — reads upload_meta.json, uploads to YouTube
- **Step 8 (cleanup)**: deletes renders dir + state file

## 3. Where Overlay Outputs Go (Directories)

### Intermediate dirs

| Path | Contents | Lifetime |
|---|---|---|
| `{work_dir}/` | `sprites/*.png`, `kb_temp.mp4`, `kb_fc.txt`, `pip_fc.txt` | Temp dir, `shutil.rmtree()` in `finally` |
| `{video.parent}/utility_cams/unnamed/{util_slug}/{demo_id}/` | `flight_*.mp4`, `_throw_poses.json` | **Persistent cache** (never cleaned) |
| `{video.parent}/utility_cams/` (walk up to 5 parents) | Pre-rendered clips from prior runs | Persistent cache |

### Final dirs

| Path | Contents | Created by |
|---|---|---|
| `youtube/{run_id}/video.mp4` | Raw POV (no overlay) | step 3 concat |
| `youtube/{run_id}/video.overlay.mp4` | Overlay sidecar (raw mode) OR overlay variant replaces video.mp4 (dual-upload) | step 4 overlay |
| `youtube/{run_id}_overlay/video.mp4` | (dual-upload only) Overlay-enhanced video | step 4 overlay |
| `youtube/{run_id}_overlay/upload_meta.json` | Separate title/desc/tags | step 6 thumbnail |

### Dual-upload variant details

- `self.dual_upload` = True from `--dual-upload` flag
- `self.overlay_youtube_dir = self.youtube_dir.with_name(self.youtube_dir.name + "_overlay")`
- step_concat copies combined.mp4 to **both** `youtube/{run_id}/video.mp4` AND `youtube/{run_id}_overlay/video.mp4`
- step_overlay: in dual-upload mode, overlay_pov.py writes sidecar, then pipeline replaces video.mp4 with sidecar **in overlay dir only**
- step_outro: appends outro to both dirs independently
- step_thumbnail: generates two thumbnails, writes two upload_meta.json
- step_upload: two variants, each with its own state key (`youtube_id`, `overlay_youtube_id`)

## 4. What Makes Overlay Expensive

### Flight clip rendering (dominant cost)

Each throw requires a CSDM render via HLAE:
- `build_flight_command(job, render_dir)` → builds CSDM video command
- `run_csdm(cmd, label, ...)` → **spawns inject poll thread** that writes `spec_goto` chase-cam commands into csdm actions JSON while CS2 runs
- Flight clips render at **1920x1080 @ 64fps** (uses h264_nvenc)
- Each clip: ~1-2 minutes runtime (depends on flight duration)
- A full match with 20+ throws = **30-60 minutes budget**
- CSDM/HLAE must run sequentially (one CS2 instance)
- **Steam must be running** — checked by csdm

### ffmpeg composite (moderate cost)

Two passes if both keyboard + PiP clips:
- Pass 1: keyboard overlay (1 vid + 18 sprite PNG inputs)
- Pass 2: PiP composite (1 vid + N flight clip inputs)
- **h264_nvenc** with fallback to libx264 if NVENC fails
- Each pass: 5-15 minutes for full match

### Keyboard extraction (cheap, <2s)

- demoparser2 `parse_ticks()` with REQUIRED_TICK_FIELDS
- Per-round frame-to-tick mapping using sidecar data

## 5. Determinism / Idempotency Matrix

| Sub-operation | Deterministic | Idempotent | Can skip on resume? |
|---|---|---|---|
| Video probe | Yes | Yes | Always cheap, no need |
| Round tick ranges | Yes (from parquet) | Yes | If cached in state |
| POV play ranges | Yes | Yes | If cached in state |
| Keyboard extraction | Yes (same demo+player) | Yes | If per_sig cached; cheap enough to redo |
| Sprite PNGs | Yes | Yes | Deterministic paths in tempdir |
| Flight clip render (CSDM/HLAE) | **No** (real-time render) | **Only if >100KB exists** | **SKIP if clip exists** |
| Flight clip scan (utility_cams) | Yes | Yes | Cheap scan per throw |
| ffmpeg composite | Yes (deterministic inputs) | Yes (overwrite) | Only needed if inputs change |
| overlay.overlay.mp4 write | Yes | Yes (overwrite) | CHECK if exists + valid |

### Current partial resume already exists in flight renders

In `_render_throw_flight_clips()`:
```python
if clip_path.is_file() and clip_path.stat().st_size > 100_000:
    _log(f"  [flight] {util_type} throw {idx} already rendered, skipping")
```

This means if an earlier pipeline run rendered some flight clips (stored in utility_cams), a retry won't re-render them. **However**, the `_find_demo_data_dir()` and trajectory data reload are still done on each run.

### What's NOT resumable

1. **No tracking of which throws have been rendered** in `.pipeline/{run_id}.json`
2. **No checkpoint between flight renders** — crash on throw 15/20 means throws 1-14 were rendered (cached in utility_cams) but pipeline state still shows step 4 with no sub-step progress
3. **No partial overlay output** — `video.overlay.mp4` is only written at the very end; if crash during FFmpeg, no sidecar exists
4. **No intermediate concat state** — `kb_temp.mp4` written to temp work dir (deleted on crash)
5. **No resume of dual-upload variant separately** — if overlay crashes, dual-upload mode can't distinguish "raw already uploaded, need overlay only"

## 6. CS2UtilArchive Data Sources

### throws.parquet schema

Location: `{CS2UtilArchive}/results/{project}/data/demo={demo_id}/throws.parquet`

Columns (from code inference):
- `throw_id` — unique throw identifier (string, format: `{demo}:e{entity}:s{segment}`)
- `thrower_steamid` — int64 Steam64
- `throw_tick`, `land_tick`, `detonate_tick` — tick numbers
- `flight_ticks` — frame count of flight
- `util_type` — "flash", "he", "smoke", "fire", "decoy"
- `round_num` — round number (1-indexed)
- `throw_x/y/z`, `release_x/y/z`, `throw_pitch/yaw` — spatial data
- `map`, `demo_id`, `thrower_side` — metadata

Other parquet files in same dir:
- `trajectories.parquet` — per-tick nade positions (columns: `throw_id`, `tick`, `x`, `y`, `z`)
- `rounds.parquet` — round boundaries (`round_num`, `end_tick`)
- `input_overlay.parquet` — pre-extracted keyboard states (per-throw tick-level, columns: `demo_id`, `throw_id`, `tick`, `{key}_state`, `{key}_conf`)
- `flash_blind_victims.parquet` — flash victim data

### Input overlay parquet

Used as alternative data source for keyboard overlay. Contains per-throw-window tick-level button states. Columns: `demo_id`, `throw_id`, `tick`, `w_state`, `a_state`, ..., `walk_state`, `w_conf`, ..., `walk_conf`.

Current overlay_pov.py does NOT use this pre-extracted parquet — it does its own demoparser2 full extraction. The `extract_input_overlay.py` script in CS2UtilArchive creates it.

### trajectories.parquet

Used for flight camera injection. Loaded in `_render_throw_flight_clips()` → grouped by `throw_id` → passed as `flight_trajectories_df` to `run_csdm()` → `_inject_camera_flight()`.

## 7. demoparser2 Keyboard Extraction

### Code path

`_extract_keyboard_states()`
→ `DemoParser.parse_ticks(list(REQUIRED_TICK_FIELDS), players=[steam_id])`
→ `overlay_tick_from_row(row, apply_jump_inference=False)`
→ Returns `{signal_name: [0/1 per frame], ...}`

REQUIRED_TICK_FIELDS = `("tick", "steamid", "FORWARD", "LEFT", "RIGHT", "BACK", "FIRE", "RIGHTCLICK", "WALK", "ducked", "ducking", "in_duck_jump", "old_jump_pressed", "buttons", "is_airborne", "velocity_Z")`

### Frame-to-tick mapping

Uses `round_offsets` sidecar (JSON written by concat_rounds.py) to map video frames back to demo ticks per-round:
- `round_offsets[round_num]` = start second of round in video
- `round_video_duration[round_num]` = duration of round in video
- `round_tick_ranges[round_num]` = (start_tick, end_tick) in demo
- Maps: `tick = rs + int((offset_sec / vid_dur) * tick_range)`

This is deterministic given the same inputs.

## 8. CSDM Flight Command Construction

### `build_flight_command()` (in CS2UtilArchive scripts/render/csdm.py)

```python
cmd = [
    CSDM, "video", demo_path, str(throw_tick), str(flight_end),
    "--output", util_dir,
    "--output-file-name", f"{clip_name}.mp4",
    "--recording-system", "HLAE",
] + BASE_FLAGS  # 1920x1080@64fps, h264_nvenc, CQ 18
```

Flight clip ends at `detonate_tick + 128` ticks.

### Camera injection via `run_csdm()`

`run_csdm()` spawns `_poll_actions_while_running()` in a daemon thread that:
1. Monitors CSDM actions JSON file (`{demo_path}.json`)
2. When file appears, injects `spec_goto` commands via `_inject_camera_flight()`
3. Uses `flight_trajectories_df` (from trajectories.parquet) for chase cam positions
4. Uses `flight_throw_pose` for initial camera position/aim
5. `flight_smooth=0.75` controls camera smoothing

This injection is the reason flight clips are **non-deterministic** (real-time rendering) and must run one at a time.

## 9. Potential Resumability Approach

### What can be persisted to `.pipeline/{run_id}.json`

```json
{
  "step": 4,
  "data": {
    "overlay_progress": {
      "throws_attempted": ["throw_id_1", "throw_id_2", ...],
      "throws_completed": ["throw_id_1", ...],
      "throws_skipped": ["throw_id_3", ...],   // no trajectory data
      "keyboard_done": true,
      "sprites_done": true,
      "flight_renders_completed": ["throw_id_1", "throw_id_2", ...],
      "composite_done": false,
      "overlay_output_path": "path/to/video.overlay.mp4",
      "overlay_output_md5": "abc..."
    }
  }
}
```

### Resume checkpoints

1. **Pre-overlay**: Check if `video.overlay.mp4` exists + valid (probe duration) → skip entire overlay
2. **Pre-keyboard**: Check `keyboard_done` flag → skip keyboard extraction
3. **Per-throw flight**: Check `throws_completed` list → skip already-done throws (utility_cams cache is the current mechanism, but state tracking avoids reloading trajectories for completed throws)
4. **Post-flight-render / Pre-composite**: Check `flight_renders_completed` length matches throw count → skip rendering, jump to composite
5. **Composite done**: Check `composite_done` flag → skip ffmpeg

### Risk areas

- **Flight clip output naming**: If `flight_clip_name()` depends on throw_id, path is deterministic. Already consistent.
- **utility_cams cache lifecycle**: Never cleaned. Safe to depend on.
- **parallel CS2/HLAE**: Must not run overlapping renders. Pipeline's single-threaded execution is fine.
- **state file corruption**: If crash during `save_state()`, state JSON is truncated. Use atomic write (write to temp, rename).
- **dual-upload race**: Each variant needs independent progress tracking. Use separate state keys or path-dependent state.
