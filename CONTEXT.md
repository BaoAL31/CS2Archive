# Scripts Directory Map

## Overview
29 `.py` files in `scripts/`. Pipeline entry point is `pipeline.py`, backlog creation via `create_backlog.py`. Step scripts for each pipeline stage exist for debugging/resume.

---

## .py Files (alphabetical)

| File | Summary |
|---|---|
| `_check_hf.py` | List files in HF dataset `cs2povarchive/cs2-demos/iem_cologne_major_2026`; writes to `_hf_output.json` |
| `_diff_check.py` | Compare two images pixel-by-pixel; checks animation frame differences (debug tool) |
| `assign_playlist.py` | Normalize tournament names to playlist-safe format |
| `batch_upload_hf.py` | Batch-upload IEM Cologne 2026 demos to HF with resume, N per run, cleanup after |
| `best_per_map.py` | Print highest-rated player per team per map from ratings JSON |
| `check_youtube_status.py` | Query YouTube API to inspect upload/publish status of videos |
| `cleanup_renders.py` | Delete renders folder after confirming video lives in youtube/ |
| `concat_rounds.py` | Concatenate batch MP4s → `combined.mp4` (incremental batch-by-batch, upscale to 1440p) |
| `create_backlog.py` | **Backlog creator:** download demo, scrape ratings, resolve steam IDs, fetch avatars, write per-player JSON cards |
| `debug_flight_render.py` | Debug: render one utility flight clip with proper chase-cam injection (CS2UtilArchive) |
| `dl.py` | Manually download specific missing HLTV matches by ID |
| `dl_missing.py` | Parallel-download remaining undownloaded matches (no state file) |
| `extract_steamids.py` | Extract all Steam64 IDs from a `.dem` file via csdm |
| `generate_outro.py` | Generate 5s silent outro clip (black + centered text) for POV videos |
| `generate_title.py` | Generate YouTube title/description JSON from ratings data |
| `overlay_pov.py` | **Overlay stage:** keyboard sprite overlay (demoparser2) + utility throw flight PiP (bottom-left) |
| `pipeline.py` | **Main pipeline orchestrator:** steps 1-8 (analyze→render→concat→overlay→outro→thumbnail→upload→cleanup) |
| `pipeline_chain.py` | Chain pipelines: start next POV when previous hits upload (step >= 6), poll-based |
| `rename_legacy_rounds.py` | One-time migration: rename `round-NNN.mp4` → `batch-NNN-NNN.mp4` |
| `render_pov.py` | **Render stage:** batch-render player POV via csdm+HLAE, crosshair swap, split-demo handling |
| `test_pip_burnin.py` | Isolated test for PiP burn-in (utility flight overlay filter chain) |
| `test_util_render.py` | Test: render single utility throw via CS2UtilArchive into `renders_test/` |
| `update_video.py` | Update existing YouTube video title/description/thumbnail |
| `upload_demos_to_hf.py` | Parallel-upload `.dem` files to HF dataset with resume state |
| `upload_hf_demos.py` | Sequential HF upload of demo folders (old approach, simpler) |
| `upload_hf_demos_git.py` | HF upload via git-lfs workflow |
| `upload_youtube_shorts.py` | Upload YouTube Short with optional scheduled publish |
| `upload_youtube.py` | **Upload stage:** YouTube upload with thumbnail, resumable, auto-schedule next 16:30 AEST slot |
| `youtube_schedule.py` | Timezone-aware publish scheduling helpers (wall-clock → UTC, Windows IANA mapping) |

---

## Key Pipeline Flow

```
create_backlog.py ← entry (download + metadata)
       ↓
pipeline.py --step 1  → csdm analyze
pipeline.py --step 2  → render_pov.py (csdm + HLAE)
pipeline.py --step 3  → concat_rounds.py
pipeline.py --step 4  → overlay_pov.py (optional)
pipeline.py --step 5  → generate_outro.py
pipeline.py --step 6  → thumbnail generation
pipeline.py --step 7  → upload_youtube.py
pipeline.py --step 8  → cleanup_renders.py
```

## Subdirectories
- `__pycache__/` — Python bytecode cache (excluded from VCS)
- No other subdirectories

## Non-Python files
- `_check_hf.bat` — batch wrapper for `_check_hf.py`
- `_test_simple.bat` — test batch script
- `.upload_hf_state.json` — state file for HF upload resume
