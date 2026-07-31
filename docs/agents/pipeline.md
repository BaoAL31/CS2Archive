# Pipeline — Deep Dive (POV Archive)

> Reference doc extracted from `AGENTS.md` (pipeline section). Read this when running, resuming, chaining, or debugging the pipeline; `AGENTS.md` keeps only the quick reference.

## Pipeline (Primary Entry Point)

`python scripts/pov/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--until N]`

Reads all POV metadata from the backlog file. Runs steps 1-6 in order (analyze → render → concat → overlay → outro → thumbnail), then writes `upload_meta.json` for each variant. **The pipeline does NOT upload** — a separate pass (`scripts/upload/upload_pending.py`) uploads every pending `upload_meta.json` under `youtube/`. Resumable — state saved to `.pipeline/{run_id}.json`. Use `--step N` to start at a specific step.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | analyze | `csdm analyze <demo>` |
| 2 | render | `python scripts/pov/render_pov.py <demo> <steam_id>` |
| 3 | concat | `python scripts/pov/concat_rounds.py <renders_folder>` |
| **4** | **overlay** | `python scripts/overlay/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N]` |
| 5 | outro | `python scripts/pov/generate_outro.py <video.mp4>` |
| 6 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --demo <dem> --steam-id <id>` |
| 7 | cleanup | `python scripts/pov/cleanup_renders.py <renders_folder>` |

**Uploading is a separate step.** The pipeline stops at step 6 (thumbnail) and writes `upload_meta.json` (with `youtube_id=null`, `upload_status="pending"`) for every variant. Run `python scripts/upload/upload_pending.py` afterward to upload all pending metas (it scans `youtube/*/upload_meta.json` and uploads any not yet completed). Step 4 (overlay) runs by default because dual-upload is on. To skip overlay entirely, run with `--until 3`. Raw-only mode (`--raw-only`) skips step 4 entirely — no overlay work directory or util_cams are created.

**Overlay step (step 4) does two things:**
1. Extracts keyboard states via demoparser2 (full round, not sparse parquet)
2. Renders utility throw flight clips via CSDM `build_flight_command()` (chase camera) then composites as PiP overlays at bottom-left

Throw clips are rendered in sequence via CSDM/HLAE — this takes ~1-2 minutes per throw. For a full match with 20+ throws, budget 30-60 minutes.

### Structured Errors (agent-parseable)

Every pipeline step validates its output and exits with a single JSON error line on failure:

```
[PIPELINE_ERROR] {"error":true,"step":1,"step_name":"analyze","code":"ANALYZE_NO_ROUNDS","message":"csdm analysis has zero rounds"}
```

Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling (e.g. `RENDER_STEAM_NOT_RUNNING`, `OUTRO_CONCAT_FAILED`, `THUMBNAIL_MISSING`). Upload errors come from `upload_pending.py` / `upload_youtube.py` (e.g. `UPLOAD_NO_VIDEO_ID`), not the pipeline.

### Example

```
# 1. Pipeline produces finished video + thumbnail + upload_meta.json (stops at step 6)
python scripts/pov/pipeline.py --backlog backlog/spirit-vs-falcons-iem-cologne-major/high/tnir-mirage-spirit-vs-falcons-iem-cologne-major.json

# 2. Separate upload pass — uploads every pending upload_meta.json under youtube/
python scripts/upload/upload_pending.py
```

### Notes

- Pipeline reads `steam_id` from backlog entry (resolved by `create_backlog.py` from `.data/player_accounts.json`). No `--steam-id` CLI flag — source of truth is `.data/player_accounts.json` (via `python main.py player add/list`). Extract from demo: `python scripts/pov/extract_steamids.py <demo_path>`.
- `--demo` optional in pipeline: omit to download from `hltv_url` (CloakBrowser) or from HuggingFace if `hf_root` set; pass `.rar` or `.dem` to skip download. `--force` re-downloads.
- **HF auto-download:** if `demo_path` not found locally and backlog has `hf_root` (e.g. `iem_cologne_major_2026`), pipeline pulls single `.dem` from `cs2povarchive/cs2-demos` dataset. Demo-level granularity — only the needed map is downloaded.
- Render step verifies Steam is running before starting.
- Each step validates its output before proceeding — failures halt the pipeline.
- **Resume rule: ALWAYS check `.pipeline/{run_id}.json` before deleting any saved progress (combined.mp4, rendered clips, etc.). The pipeline state tells you which step was last completed. Run `python scripts/pov/pipeline.py --backlog <path> --step <N>` (same backlog) to resume.
- **Render folder per POV** — `renders/pov-{demo-stem}_{player}/` (not demo-only). Multiple POVs on the same map share the match demo folder but never share a render folder. Legacy `pov-{demo-stem}/` (no player suffix) may still exist from older runs; safe to delete after confirming youtube output.
- **`--resume-from-round N`** — deprecated. Render now uses filesystem-based resume: existing `batch-*.mp4` files ≥1MB are automatically skipped on re-run. To re-render a specific batch, manually delete its file.
- **`--batches N`** — number of render batches (default: 2). Rounds are divided equally across N batches; the last batch gets fewer rounds if they don't divide evenly. Each batch produces one MP4 named `batch-{start:03d}-{end:03d}.mp4`. `--batches 1` renders all rounds in a single CSDM call.
- **`--until N`** — stop after step N (e.g. `--until 5` runs through outro, skips thumbnail/cleanup). Default: run through step 6 (thumbnail; upload handled separately by `upload_pending.py`).
- **`--skip-failed-rounds`** — **[DANGER] NEVER set by default.** Skip round batches that fail during rendering instead of aborting the entire pipeline. Only use when a specific demo file is corrupted/incompatible (like the `100-thieves-vs-spirit-m3-dust2.dem` from BLAST Bounty 2026 Season 2 — that demo fails at round 1 with "Game error" for every player). Silently drops failed rounds, producing an incomplete POV video. Enabled per-invocation via CLI flag or the backlog entry's `pipeline_cmd` when the demo is known-broken. See backlog `skip_failed_rounds: true` entries for the canonical example.
- **`--dual-upload`** — now the **default**: dual-upload is ON unless you pass `--raw-only`. Produces a second independent variant with the keyboard + util-cam overlay. Raw-only mode is the opt-in.
- **`--overlay-only`** — render/upload only the overlay variant. Implies `--dual-upload`'s overlay branch but skips raw video copy / raw outro / raw thumbnail. No `youtube/{run_id}/` dir created. Use when you only want the keyboard+util-cam version on the channel. State stored under `overlay_only=True` for resume.

### Dual-Upload (`--dual-upload`)

By default (and whenever `--dual-upload` is in effect), the pipeline produces **two** separate uploads from one backlog entry:

| Variant | YouTube dir | Title suffix | Thumbnail | Description |
|---|---|---|---|---|
| Raw | `youtube/{run_id}/` | _(none)_ | standard | standard |
| Overlay | `youtube/{run_id}_overlay/` | `\| Input Overlay + Utility Cam` | + `W/ INPUT OVERLAY` badge top-right | + overlay note paragraph |

Both variants get independent `upload_meta.json`; `upload_pending.py` records each variant's YouTube video ID in its own meta file and reserves independent publish-schedule slots.

**Data flow:**
1. Step 3 (concat): `combined.mp4` copied to both `youtube/{run_id}/` and `youtube/{run_id}_overlay/`
2. Step 4 (overlay): runs `overlay_pov.py` on the overlay dir's `video.mp4`; output replaces `video.mp4` in the overlay dir. Skipped in raw-only mode (`--raw-only`) so cost is zero.
3. Step 5 (outro): appended to both `video.mp4` files
4. Step 6 (thumbnail): two thumbnails generated, each with its own `upload_meta.json`
5. Upload: handled by a separate `upload_pending.py` pass — both variants uploaded, each from its own `upload_meta.json`, skipped if already completed.
6. Step 7 (cleanup): unchanged

**Resume:** Upload is resume-safe: `upload_pending.py` skips any `upload_meta.json` whose `upload_status == "completed"` (youtube_id set), so re-running only uploads what's left. Re-running the pipeline with the same `--dual-upload` flag re-runs only the missing render/overlay/thumbnail work.

**Cost:** raw-only mode (`--raw-only`) skips the ~30–60 min overlay render (20+ throws) and the extra upload. Default dual-upload adds both.

**Raw-only mode (`--raw-only`):** step 4 is skipped entirely — no overlay work directory or util_cams are created. The only state key is `dual_upload=False`. Existing `youtube/{run_id}/` and `.pipeline/{run_id}.json` files are otherwise unaffected.

**`--overlay-only`** is a strict subset of `--dual-upload` for the overlay branch. Resuming a failed overlay-only run with the same flag re-runs only the missing overlay work; no raw artifacts are ever produced.

### Bilibili Mirror & Other Utilities

These tools sit outside the core `pipeline.py` → `upload_pending.py` flow but are wired into it:

- **Bilibili mirror** — `scripts/upload/upload_bilibili.py` uploads the same `upload_meta.json` variants to `studio.bilibili.tv` (Playwright + Chrome; tags capped at 10 chips, remainder appended to description; videos >~3.8 GB re-encoded to `video_bili.mp4`). Auth: `scripts/upload/bilibili_login.py` (one-time headed-Chrome login, saves `.bilibili_storage.json`). `scripts/upload/bili_check_session.py` verifies that session. `upload_pending.py` uploads both YouTube and Bilibili pending metas via `is_bilibili_pending`.
- **HuggingFace demo sync** — `scripts/hf/upload_demos_to_hf.py` (parallel, resumable), `scripts/hf/upload_hf_demos.py`, `scripts/hf/upload_hf_demos_git.py` (git-lfs), and `scripts/hf/batch_upload_hf.py` (resumable IEM Cologne Major 2026 batch push) upload `.dem` files to `cs2povarchive/cs2-demos`, which backs the pipeline's `hf_root` auto-download.
- **Shorts & scheduling** — `scripts/upload/upload_youtube_shorts.py` (Short upload with optional scheduled publish) and `scripts/upload/youtube_schedule.py` (timezone-aware wall-clock → UTC helpers used by `upload_youtube.py`).
- **Render helpers** — `scripts/pov/crosshair_code.py` (CS2 share-code ↔ crosshair decode/encode) and `scripts/pov/cs2_minimizer.py` (minimizes the CS2 window to stop focus theft during HLAE capture).

### Chaining pipelines (upload overlap)

Use `scripts/pov/pipeline_chain.py` to start the **next** POV when the **previous** reaches **thumbnail/upload-ready** (state `step >= 6`). Only one render (step 2) should run at a time.

**How it works:** polls `.pipeline/{run_id}.json` every 30s (`--poll`). When `"step" >= 6`, spawns `pipeline.py` with the args you pass after `--`. Does **not** read terminal output — only the state file. The pipeline stops at step 6 (thumbnail) with `upload_status="pending"`; uploading is a separate `upload_pending.py` pass that can overlap with the next POV's render.

**`run_id`** = `{match_id}_{demo_stem}_{player}_{map}` (e.g. `2394174_falcons-vs-mouz-m3-nuke_NiKo_Nuke`). Includes HLTV match ID to prevent collision when the same teams/map/player appear in different tournaments. Use `match_id_from_url()` from `scrapers/hltv_acquire.py` to extract the ID. Same args as the watched pipeline must be used when resuming.

```powershell
# When NiKo hits upload, start kyousuke (chain exits after launch)
python scripts/pov/pipeline_chain.py --watch falcons-vs-mouz-m3-nuke_NiKo_Nuke --no-wait -- `
  --backlog backlog/falcons-vs-mouz-cs-asia-championships-2026/high/kyousuke-dust2-falcons-vs-mouz-cs-asia-championships-2026.json

# Chain xelex after kyousuke (run in a second terminal)
python scripts/pov/pipeline_chain.py --watch falcons-vs-mouz-m2-dust2_kyousuke_Dust2 --no-wait -- `
  --backlog backlog/falcons-vs-mouz-cs-asia-championships-2026/high/xelex-mirage-falcons-vs-mouz-cs-asia-championships-2026.json
```

- **`--no-wait`** — start the next pipeline and exit (recommended; each POV runs in its own process/terminal).
- Omit `--no-wait` to block until the spawned pipeline finishes.
- Start POV A's pipeline first, then start chain watcher(s) in separate background terminals.

## Backlog Creation

`python scripts/pov/create_backlog.py <hltv_url>` — downloads a match and generates prioritized backlog entries for every player/map combo.

**Demos are downloaded automatically.** The script calls into `acquire_match()` then scrapes HLTV Rating 3.0, creating a per-player backlog card ranked by rating. It validates that the `.dem` file for each map exists on disk — if not found, it raises `FileNotFoundError` with the expected path, rather than writing a placeholder.

Each backlog entry contains full metadata as JSON: player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority. The script also scrapes tournament name from HLTV, fetches player avatars, and adds `hf_root` for HuggingFace demo auto-download.

```powershell
# 1. Download demo + create backlog entries (all-in-one)
python scripts/pov/create_backlog.py "https://www.hltv.org/matches/2394998/g2-vs-spirit-iem-cologne-major-2026"
```

Backlog entries land in `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` with the simplified pipeline command. Use these as handoff cards for running pipelines.

## Output Directory Structure

After completing the pipeline for a POV:
```
youtube/
└── {match-slug}_{player}_{map}/
    ├── thumbnail.png       (1280×720 PNG, auto-generated)
    ├── video.mp4           (1080p60, full match POV, concatenated rounds)
    └── upload_meta.json    (title, description, tags, upload status, youtube_id)
```

With `--dual-upload`, a second variant is added:
```
youtube/
└── {match-slug}_{player}_{map}_overlay/
    ├── thumbnail.png       (1280×720 PNG, with W/ INPUT OVERLAY + + UTIL CAMS badges bottom-right)
    ├── video.mp4           (overlay-enhanced POV, same dimensions, with keyboard + util cam)
    └── upload_meta.json    (title suffix "| Input Overlay + Utility Cam", extra tags, overlay note in description)
```

**Overlay thumbnail background (canon):** for `variant=overlay`, the pipeline extracts a single frame from `combined.overlay.mp4` (the actual overlay video) at ~40% duration, scales to 1920×1080, and passes it to the thumbnail CLI as `--background`. The keyboard overlay and util-cam PiP are faintly visible behind the Gaussian blur — proof the variant has real overlay content. Falls back to kill-frame extraction (from raw demo) if the overlay video is missing.
