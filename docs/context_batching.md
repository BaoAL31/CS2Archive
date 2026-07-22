# Batched Overlay Encoding — Full Context

## 1. The Problem

`overlay_pov.py` applies keyboard input overlay + utility throw flight PiP as a **single monolithic ffmpeg encode**. For a full match (~20-25 rounds), the filter_complex has **hundreds of overlay nodes** in series (one per key-press/release segment per signal). ffmpeg evaluates them serially per frame → 3hr wallclock for a 40-min video.

**Batching solves two problems:**
1. Smaller filter graph per batch (fewer segments) → faster per-frame throughput → 2-3x speedup
2. Filesystem resume: crash mid-way loses only current batch

## 2. File Map

| File | Role |
|---|---|
| `scripts/overlay/overlay_pov.py` | Entry point. `run_overlay()` orchestrates keyboard extraction → sprite gen → flight clips → ffmpeg composite. |
| `scripts/pov/pipeline.py` | Pipeline runner. `step_overlay()` calls `overlay_pov.py` as subprocess, handles dual-upload. |
| `scripts/pov/concat_rounds.py` | Batch concat pattern (reference). Concat `batch-*.mp4` → `combined.mp4` with filesystem resume. |
| `D:/Projects/CS2UtilArchive/scripts/render/overlay_assets.py` | `build_png_overlay_filter()` — generates filter_complex string from `per_sig` dict. |
| `D:/Projects/CS2UtilArchive/scripts/render/overlay_layout.py` | Layout constants, `_OVERLAY_SIGNALS`, `_iter_inclusive_runs()`. |
| `D:/Projects/CS2UtilArchive/scripts/input_overlay_decode.py` | `decode_button_mask()`, `overlay_tick_from_row()`. |

## 3. `overlay_pov.py` Data Flow (run_overlay)

```
run_overlay(video_path, demo_path, steam_id, round_num=None)
  │
  ├─ _probe_video_info(video_path) → (width, height, fps, frame_count)
  │
  ├─ Load round_offsets.json sidecar (searches video_path.stem.round_offsets.json next to video)
  │   │   round_offsets: dict[int, float]  — round_num → start_seconds_in_video
  │   │   round_video_duration: dict[int, float]  — round_num → video_duration_seconds
  │   │   video_total_seconds: float
  │   │   batches: list[{round_start, round_end, duration_seconds}]
  │   │   per_round_ticks: dict[int, [start_tick, end_tick]]  (optional, from CSDM sequence files)
  │   │   per_round_durations: dict[int, float]  (optional)
  │   │
  │   └─ If sidecar NOT found (round_offsets = {}):
  │       → Falls through to linear tick mapping (WRONG alignment)
  │       → BUG: pipeline.py does NOT copy round_offsets.json to youtube dir
  │
  ├─ _load_round_tick_ranges(demo_path) → dict[int, (start_tick, end_tick)]
  │   (from rounds.parquet → demoparser2 round_start events)
  │
  ├─ _load_pov_play_tick_ranges(demo_path, steam_id) → dict[int, (start_tick, end_tick)]
  │   (trimmed to freeze_end/death, matching CSDM's actual recording range)
  │
  ├─ Step 1: _extract_keyboard_states(demo, steam_id, frame_count, fps, ...)
  │   │   → per_sig: dict[str, list[int]]  — 9 signals × frame_count entries (0/1)
  │   │   Uses round_offsets + round_tick_ranges for per-frame→per-tick mapping
  │   │   When round_offsets missing: linear mapping from base_tick
  │   └─ per_sig[sig][f_idx] = 0 or 1 for each frame
  │
  ├─ Step 2: generate_key_assets(work_dir/"sprites") → assets dict
  │   │   overlay_png_input_paths(assets) → list[Path]  — 18 PNGs (9 idle + 9 pressed)
  │   │   build_png_overlay_filter(per_sig, ...)
  │   │     → keyboard_fc: str  (filter_complex graph)
  │   │     → keyboard_out_label: str  (e.g. "[ov48]")
  │   │
  │   └─ FILTER GRAPH SIZE: per_sig has 9 signals × up to ~200 segments each
  │       → 200+ overlay nodes chained serially → 3hr encode
  │
  ├─ Step 3: _render_throw_flight_clips(...)
  │   │   → list[PipClip]: {clip_path, start_frame, end_frame, util_type, pip_index}
  │   │   Uses filesystem cache: skip if clip >100KB (already resumable)
  │   │   ~1-2 min per throw via CSDM/HLAE
  │   └─ Returns sorted by start_frame
  │
  └─ Step 4: ffmpeg composite
      │
      ├─ Keyboard + Flight: 2-pass
      │   Pass 1: ffmpeg -i video.mp4 -i sprites... -filter_complex KEYBOARD_FC → kb_temp.mp4
      │   Pass 2: ffmpeg -i kb_temp.mp4 -i flight1.mp4... -filter_complex PIP_FC → video.overlay.mp4
      │
      ├─ Keyboard only: 1-pass → video.overlay.mp4
      ├─ Flight only: 1-pass → video.overlay.mp4
      └─ None: return early
```

## 4. `round_offsets.json` Schema

Written by `concat_rounds.py` at `renders/{pov_dir}/combined.round_offsets.json`:

```json
{
  "total_rounds": 24,
  "total_duration_seconds": 1520.123,
  "round_offsets": {"1": 0.0, "2": 62.5, "3": 125.1, ...},
  "batches": [
    {"batch": "batch-001-010.mp4", "round_start": 1, "round_end": 10, "duration_seconds": 625.0},
    {"batch": "batch-011-020.mp4", "round_start": 11, "round_end": 20, "duration_seconds": 595.0},
    ...
  ],
  "per_round_ticks": {"1": [tick_start, tick_end], ...},
  "per_round_durations": {"1": 62.5, ...}
}
```

**Key fields for batching:**
- `round_offsets`: `{round_num: start_seconds_in_video}` → convert to frame: `start_frame = int(round_offsets[rn] * fps)`
- `batches[i]["duration_seconds"]`: total video duration of that batch
- `per_round_durations`: actual per-round video seconds (from CSDM sequence file probes, ground truth)

**BUG:** pipeline.py step_concat copies only `combined.mp4` → `youtube/{run_id}/video.mp4` but **NOT** `combined.round_offsets.json`. overlay_pov.py looks for `video.round_offsets.json` next to the video — it doesn't exist → falls to linear tick mapping.

## 5. `per_sig` Structure and Slicing

```python
per_sig: dict[str, list[int]] = {
    "w": [0, 0, 0, 1, 1, 0, 0, ...],  # len = frame_count
    "a": [0, 0, 0, 0, 0, 0, 0, ...],
    "s": [...],
    "d": [...],
    "jump": [...],
    "duck": [...],
    "lmb": [...],
    "rmb": [...],
    "walk": [...],
}
```

**Slice for batch covering frames [start, end):**
```python
batch_per_sig = {sig: per_sig[sig][start_frame:end_frame] for sig in per_sig}
```
Pass to `build_png_overlay_filter(batch_per_sig, ...)` — the function computes segments from `_iter_inclusive_runs()` internally, so shorter input = fewer segments = smaller filter graph.

**Important:** `build_png_overlay_filter()` uses `per_sig_states[sig]` only for segment detection. The filter uses `enable='between(n, seg_start, seg_end)'` which is **zero-relative to this input**. Slicing to [0, N) is correct — the overlay positions at frame 0 of the segment.

## 6. `build_png_overlay_filter()` Signature

```python
def build_png_overlay_filter(
    per_sig_states: dict[str, list[int]],   # sliced per batch — list length = batch frame count
    *,
    assets: dict[str, dict[str, Path]],     # from generate_key_assets()
    placement: str,                          # "bottom-center"
    video_width: int,
    video_height: int,
    video_label: str = "[0:v]",             # video input tag
    png_input_offset: int = 1,              # first PNG is input 1 (after video input 0)
    pressed_release_fade_frames: int = 12,   # fade duration
    pressed_release_fade_steps: int = 4,     # fade granularity
) -> tuple[str, str]:  # (filter_complex_string, output_label)
```

**Filter string structure:**
```
[0:v][1:v]overlay=x=10:y=20[ok0b];
[ok0b][3:v]overlay=x=10:y=20:enable='between(n\,10\,20)'[ps0_0];
[ps0_0][1:v]overlay=x=30:y=80[ok1b];
...
```

Each overlay node chains from previous output. The `n` in `enable` is **zero-relative to the input video** — matching sliced per_sig.

## 7. PiP Frame Positioning (`_build_pip_overlay`)

```python
def _build_pip_overlay(
    clip: PipClip,          # {clip_path, start_frame, end_frame, util_type, pip_index}
    current_label: str,
    input_idx: int,
    width: int, height: int,
    fps: float,
) -> tuple[str, str]:  # (filter_part, output_tag)
```

Key technique: **PTS delay instead of enable**:
```python
start_seconds = clip.start_frame / fps
pre_filters.append(f"setpts=PTS-STARTPTS+{start_seconds:.6f}/TB")
```

This means PiP start_frame is **relative to the full video timeline**. If we batch by splitting, PiP start_frame must be **re-based to the batch's frame range**:
- Batch covers frames [batch_start, batch_end)
- For a PiP with global start_frame S, batch-local = max(0, S - batch_start)
- If S < batch_start → skip this clip for this batch
- If S >= batch_end → skip this clip for this batch

Same for end_frame: batch-local and clamped.

## 8. Pipeline `step_overlay()` Invocation

```python
def step_overlay(self) -> None:
    target_dir = self.overlay_youtube_dir if dual_upload else self.youtube_dir
    video_path = target_dir / "video.mp4"

    r = self._run_py([
        "scripts/overlay/overlay_pov.py",
        "--video", str(video_path),
        "--demo", str(self.demo_path),
        "--steam-id", steam_id,
    ], timeout=7200)  # 2 hour timeout

    # After: check video.overlay.mp4 sidecar
    overlay_sidecar = video_path.with_suffix(".overlay.mp4")
    # If dual-upload: overlay_sidecar replaces video.mp4
```

**Missing:**
1. `round_offsets.json` not copied to youtube dir
2. No `--batches` flag passed to overlay_pov.py
3. Timeout 7200s barely fits full non-batched run (3hr = 10800s)

## 9. `concat_rounds.py` Batch Resume Pattern (Reference)

```python
# Filesystem resume:
# - combined.mp4 exists? → skip first batch rename, append remaining batches
# - Each batch file deleted after successful append (not shown, from AGENTS.md)

files = sorted(glob("batch-*.mp4"), key=int(re.match(r"batch-(\d+)-\d+")))
if not combined.exists():
    files[0].rename(combined)
for f in files[1:]:
    _concat_two(combined, f, tmp)
    tmp.replace(combined)
```

Validates batch contiguity: `batch-001-010.mp4`, `batch-011-020.mp4`, etc.

Pattern for overlay batches:
- Name: `batch-overlay-001-010.mp4`
- Skip if exists + >100KB
- Concat at end via ffmpeg stream copy (no re-encode)

## 10. PiP Pipeline Input Re-mapping

Current `_ffmpeg_encode()` maps inputs:
```python
cmd = ["ffmpeg", "-y", "-i", main_input]  # input 0
for inp in extra_inputs:
    cmd.extend(["-i", str(inp)])           # inputs 1, 2, ...
```

For batched overlay, extra_inputs = [sprite PNGs + batch's flight clips]. The input index mapping:
- 0 = video segment
- 1..N = sprite PNGs (N = 18)
- N+1.. = flight clips for this batch

This must match what `build_png_overlay_filter()` expects (via `png_input_offset`) and what `_build_pip_chain()` expects (flight clip indices).

## 11. Key Design Decisions for Batching

### What to split
Split **by round batches** (default 10 rounds) using `round_offsets.json`:
```
video.mp4 → round_offsets.json → batch frame ranges
batch-overlay-001-010.mp4  (rounds 1-10 with overlay)
batch-overlay-011-020.mp4  (rounds 11-20 with overlay)
...
concat stream copy → video.overlay.mp4
```

### What to do in each batch iteration
1. Slice `per_sig[sig][start_frame:end_frame]` — shorter → smaller filter graph → faster
2. Filter `flight_clips` to those overlapping the batch frame range, re-base start_frame/end_frame
3. **Single-pass ffmpeg** (merge keyboard + PiP into one filter chain) instead of current 2-pass
4. Output: `batch-overlay-{start:03d}-{end:03d}.mp4`

### Merge keyboard + PiP into single pass
Currently: Pass 1 (keyboard) → kb_temp.mp4, then Pass 2 (PiP) → output.

Can merge:
```python
# Chain: video → keyboard overlay → PiP overlay → output
keyboard_label = "[out_kb]"  # output of keyboard filter graph
pip_chain_keyboard = f"{keyboard_label}[{pip_clip_in}]overlay=..."
```

This avoids intermediate `kb_temp.mp4` encode/decode — saves ~50% time vs 2-pass.

### Concat at end
Use `concat_rounds.py`'s pattern — ffmpeg stream copy concatinates all batches into `video.overlay.mp4`.

### Filesystem resume
- Skip batch if `batch-overlay-*.mp4` exists + >100KB
- After all batches done, concat into final output
- If concat failed mid-way, batch files remain → re-run skips completed batches

## 12. Required Changes Summary

| File | Change | LOC |
|---|---|---|
| `scripts/overlay/overlay_pov.py` | Add `--batches N` flag. Add batch loop in `run_overlay()`. Slice `per_sig`, filter PiP clips per batch. Merge keyboard+PiP single-pass ffmpeg. Batch output naming. End-of-loop concat. | ~150 |
| `scripts/pov/pipeline.py` | Copy `round_offsets.json` alongside `video.mp4` in step_concat. Forward `--overlay-batches` to `overlay_pov.py`. Check round_offsets.json exists. | ~15 |
| Total | | ~165 |

**No new files. No new dependencies. No changes to CS2UtilArchive.**
