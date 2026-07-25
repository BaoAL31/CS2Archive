# AGENTS.md — CS2Archive

> **ALWAYS use `scripts/pov/create_backlog.py` for data acquisition, then `scripts/pov/pipeline.py` for rendering, then `scripts/upload/upload_pending.py` to upload.** Individual step scripts exist for debugging or resuming a failed step — see below.

## Scripts layout

Scripts are grouped by product/concern (not a flat dump):

| Folder | Contents |
|---|---|
| `scripts/pov/` | POV Archive pipeline (`pipeline.py`, render, concat, backlog, …) |
| `scripts/overlay/` | Keyboard + util-cam overlay |
| `scripts/faceit/` | FACEIT POV helpers (titles, thumbnails, names, backlog) |
| `scripts/highlights/` | Highlight Reel / Kill Timeline (Kinocut path — separate from POV) |
| `scripts/upload/` | YouTube + Bilibili publish |
| `scripts/hf/` | HuggingFace demo sync |
| `scripts/misc/` | One-offs |

Import bootstrap: `scripts/_pathsetup.py` (`ensure()` adds all buckets to `sys.path`).

Domain glossary for the highlights product: root `CONTEXT.md`.

## Highlights (Kill Timeline) — FACEIT only

Separate product from POV Archive. v1 builds **Kill Timeline data only** (no CSDM clip renders, no Kinocut yet).

```powershell
python scripts/highlights/build_action_timeline.py demos/faceit/<demo>.dem
# -> renders/hl-{demo_stem}/action_timeline.json
```

- Hard-refuses demos outside `demos/faceit/`
- Every kill where **at least one** of attacker or victim is a Recognised Pro (`.data/player_accounts.json`). Includes unknown→pro picks.
- Recognised Pros = `player_accounts.json` only (no `faceit_pros.json`)

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
- `--demo` optional in pipeline: omit to download from `hltv_url` (CloakBrowser) or from HuggingFace if `hf_root` set; pass `.rar` or `.dem` to skip download.`--force` re-downloads.
- **HF auto-download:** if `demo_path` not found locally and backlog has `hf_root` (e.g. `iem_cologne_major_2026`), pipeline pulls single `.dem` from `cs2povarchive/cs2-demos` dataset. Demo-level granularity — only the needed map is downloaded.
- Render step verifies Steam is running before starting.
- Each step validates its output before proceeding — failures halt the pipeline.
- **Resume rule: ALWAYS check `.pipeline/{run_id}.json` before deleting any saved progress (combined.mp4, rendered clips, etc.). The pipeline state tells you which step was last completed. Run `python scripts/pov/pipeline.py --backlog <path> --step <N>` (same backlog) to resume.
- **Render folder per POV** — `renders/pov-{demo-stem}_{player}/` (not demo-only). Multiple POVs on the same map share the match demo folder but never share a render folder. Legacy `pov-{demo-stem}/` (no player suffix) may still exist from older runs; safe to delete after confirming youtube output.
- **`--resume-from-round N`** — deprecated. Render now uses filesystem-based resume: existing `batch-*.mp4` files ≥1MB are automatically skipped on re-run. To re-render a specific batch, manually delete its file.
- **`--batches N`** — number of render batches (default: 2). Rounds are divided equally across N batches; the last batch gets fewer rounds if they don't divide evenly. Each batch produces one MP4 named `batch-{start:03d}-{end:03d}.mp4`. `--batches 1` renders all rounds in a single CSDM call.
- **`--until N`** — stop after step N (e.g. `--until 5` runs through outro, skips thumbnail/cleanup). Default: run through step 6 (thumbnail; upload handled separately by `upload_pending.py`).
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

Use `scripts/pov/pipeline_chain.py` to start the **next** POV when the **previous** reaches **thumbnail/upload-ready** (state `step >= 6`). Only one render (step 2) should run at a time; the actual YouTube upload runs separately via `upload_pending.py` and can overlap with the next POV’s acquire→render.

**How it works:** polls `.pipeline/{run_id}.json` every 30s (`--poll`). When `"step" >= 6`, spawns `pipeline.py` with the args you pass after `--`. Does **not** read terminal output — only the state file.

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
- Start POV A’s pipeline first, then start chain watcher(s) in separate background terminals.

## Individual Step Scripts (Debugging / Manual Use)

Use these when debugging a specific pipeline step failure or running steps manually:

1. **Demo Download** — `python main.py hltv match <url>` (CloakBrowser, same as pipeline step 1). Archives and `.dem` files land in `demos/hltv/<match-slug>/`. Use `--force` to re-download.
2. **Ratings** — `python main.py ratings <url>` scrapes HLTV Rating 3.0 for the match (saves to `demos/analysis/`). Check top players: `python scripts/pov/best_per_map.py demos/analysis/{slug}_ratings.json`
3. **Avatars** — `scrapers/player_images.py`. Uses shared `HLTVScraper` (single Chrome instance) across all players. Navigates **player page** to capture 400×417 transparent PNG via response interception. Falls back to match page → rembg if player page fails. Rate-limited: 2s delay between players. Saved as `{nickname}.png` in `demos/avatars/`.
4. **Player Steam ID** — `python main.py player add <nickname> --steam <url>` or `python main.py player list`. Extract from demo: `python scripts/pov/extract_steamids.py <demo_path>`.
5. **CSDM Analysis** — `csdm analyze <demo>`. For PGL demos (PBDEMS2 format): `csdm analyze <demo> --source challengermode`.
6. **Render POV Clip** — `python scripts/pov/render_pov.py <demo_path> <steam_id> [--batches 2]`. Extracts player's crosshair from demo, writes to `autoexec_render.cfg`, copies over `autoexec.cfg` in CS2's cfg dir (`game/csgo/cfg/`), renders rounds in N equal batches. On exit (even crash), swaps `autoexec_personal.cfg` back. Each batch produces one `batch-{start}-{end}.mp4` file. Filesystem-based resume: existing `batch-*.mp4` files ≥1MB are automatically skipped.
7. **Concatenate Rounds** — `python scripts/pov/concat_rounds.py <renders_folder>` → `combined.mp4` (incremental batch-by-batch concat with gap/overlap validation, then upscale to 1440p). Each batch file is deleted after successful append.
8. **Render Util-Cams** — `python scripts/overlay/render_util_cams.py --util-cams-root <pov>/utility_cams --data-dir <data-dir> --demo-id <demo_id> [--steamid <id>] [--chunk-size 0]`. Uses CS2UtilArchive's **`build_player_manifest.build_manifest()`** for manifest generation (canonical entry builder) with `cameras_smoke="flight"`, `cameras_other="flight"` (overlay only needs chase-cam PiP, no thrower/detonate/orbit). Filters entries to `flight_ticks > 0`. Delegates rendering to **`render_utils.py` → `run_pipeline()`** which uses config-file batch CSDM (`render_spot_batch`) — one CS2 launch per chunk, precomputed camera inject, no thread-race issues. Output path: `<util_cams>/unnamed/<throw_id_slug>/flight_<throw_slug>.mp4` (match-id prefix stripped from `throw_id_slug`, matching CS2UtilArchive's render_utils folder architecture — no `project/map/demo_id` nesting under `utility_cams`). Idempotent — re-runs short-circuit via `_clip_index.json`.
9. **Overlay** — `python scripts/overlay/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N] [--util-cams-root <path>]`. Applies keyboard overlay (demoparser2 full extraction) + utility throw flight PiP (CSDM flight renders at bottom-left). Delegates flight clip rendering to **`render_util_cams.py`** which uses CS2UtilArchive's canonical `build_player_manifest.build_manifest()` + `render_utils.py` batch CSDM pipeline. Clips land in `<util_cams>/unnamed/<throw_id_slug>/flight_<throw_slug>.mp4` (match id stripped, aligned with CS2UtilArchive's render_utils folder architecture). Pipeline passes `--util-cams-root <render_dir>/utility_cams`. Requires CS2UtilArchive with throws.parquet & extracted demos under `demos/extracted/`.
10. **Generate Thumbnail** — `python -m thumbnail <match_url> --player <nick> --map <map> --demo <dem> --steam-id <id> [--tournament "IEM Atlanta 2026"]`. Auto-extracts random kill frame as blurred background. Or `--background <frame.jpg>`.
   Example: `python -m thumbnail "https://www.hltv.org/matches/2394166/faze-vs-vitality-iem-atlanta-2026" --player ropz --map Nuke --demo demos/hltv/.../faze-vs-vitality-m1-nuke-p2.dem --steam-id 76561197991272318 --tournament "IEM Atlanta 2026"`
11. **Generate Title & Description** — `python scripts/pov/generate_title.py <ratings_json> --player <nick> --map <map> [--tournament "..."]`. Outputs JSON with `title` and `description` from ratings data.
12. **Upload to YouTube** — `python scripts/upload/upload_youtube.py <video_path> --thumbnail <thumb.png> --title <title> --description <desc> --privacy public`. Requires Google Cloud OAuth (`client_secret.json`). First-time auth opens browser. Account must be phone-verified for custom thumbnails.
   Default publish mode is `auto`: schedule at the next future **10:00 or 16:30 Australia/Sydney** on a free day. The script queries the **YouTube API** (channel uploads playlist) for existing scheduled/published dates to find the next open slot — no local ledger is used.
   Override with `--publish-at "YYYY-MM-DD HH:MM"` for an exact time, or keep `--publish-at auto` explicit.
   Or use `--meta <upload_meta.json>` to read title/description/tags from a metadata file. The pipeline writes `upload_meta.json` at step 6 (thumbnail). The file also stores `resumable_uri`/`resumable_progress` during upload for crash recovery, and `youtube_id` after completion.
13. **Upload pending metas (batch)** — `python scripts/upload/upload_pending.py [--dry-run] [--limit N] [--dir <youtube_subdir>]`. Scans `youtube/*/upload_meta.json`, uploads any with `upload_status != "completed"` by invoking `upload_youtube.py --meta <path>` for each. Skips metas already completed (resume-safe) and metas whose video file is missing. Use after the pipeline finishes; re-running only uploads what's left.

## Backlog Creation

`python scripts/pov/create_backlog.py <hltv_url>` — downloads a match and generates prioritized backlog entries for every player/map combo.

**Demos are downloaded automatically.** The script calls into `acquire_match()` then scrapes HLTV Rating 3.0, creating a per-player backlog card ranked by rating. It validates that the `.dem` file for each map exists on disk — if not found, it raises `FileNotFoundError` with the expected path, rather than writing a placeholder.

Each backlog entry contains full metadata as JSON: player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority. The script also scrapes tournament name from HLTV, fetches player avatars, and adds `hf_root` for HuggingFace demo auto-download.

```powershell
# 1. Download demo + create backlog entries (all-in-one)
python scripts/pov/create_backlog.py "https://www.hltv.org/matches/2394998/g2-vs-spirit-iem-cologne-major-2026"
```

Backlog entries land in `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` with the simplified pipeline command. Use these as handoff cards for running pipelines.

## CLI Entry Point

`python main.py <command>` — no other entry points.

Available commands:

| Command | Purpose |
|---|---|
| `hltv match <url>` | Download demo from HLTV match page |
| `hltv player <name>` | Search & download player's recent HLTV demos |
| `faceit match <id>` | Download FACEIT demo |
| `trending [--url-only]` | Find top CS2 match videos from YouTube highlight channels, matched to HLTV |
| `ratings <url> [--top N]` | Scrape HLTV Rating 3.0 from match page |
| `player add/list/show/remove` | Manage saved player accounts (steam_id, faceit) |
| `test-pipeline` | Full integration test: ratings + avatars + demos + CS2DM analysis |
| `status` | Show download history |

## Environment

- `.env` required: `FACEIT_API_KEY`, `YOUTUBE_API_KEY`
- All config in `config.py` (pydantic-settings, loads from `.env`)
- **Python env:** uses same `cs2archive` conda env as sibling project. In non-interactive shells (OpenCode, CI), `conda activate` often fails — use direct-path bypass:
  ```powershell
  $env:PYTHONPATH="."; & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py <args>
  ```

## Demo Video Rendering

Uses **CS2 Demo Manager (csdm)** CLI to render POV videos. Installed at:
```
C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd
```

### Recording Mode

Uses `--recording-system HLAE` — csdm drives HLAE `mirv_streams` to encode directly to video via FFmpeg (no TGA/PNG sequence on disk).

**Critical:** `--output` must be an **absolute** path. Relative paths (e.g. `renders/...`) resolve from the CS2 install directory and cause `AFXERROR: Failed writing image for screen recording` → csdm **Raw files not found**. `render_pov.py` and the pipeline always pass `Path.resolve()` output dirs.

HLAE **2.190.1+** required (`C:\Program Files (x86)\HLAE\HLAE.exe`). Disable RTSS/MSI OSD and Steam/Xbox overlays if capture fails. After CS2 updates, if HLAE breaks again, test one round with absolute output before full pipeline runs.

### VP9 Trick (sharper YouTube uploads)

Render at **2560×1440** even for 1080p-targeted uploads. YouTube allocates VP9 codec (higher bitrate) to 1440p+ uploads, while 1080p gets H.264. Video looks sharper even when watched at 1080p because YouTube uses better encoding.

All scripts default to 2560×1440; per-round render and concat upscale use **h264_nvenc CQ 15** (match quality end-to-end).

### Rounds-Only POV (full HUD, no x-ray, batch rendering)

For rendering a player's POV with full HUD (radar, health, ammo) and no x-ray, in configurable batch sizes (default 3 rounds per csdm call):

```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" --mode player --steamids <steam64_id> --event rounds --rounds <N> --perspective player --no-show-x-ray --output "C:\full\path\to\renders\pov-folder" --framerate 60 --width 2560 --height 1440 --recording-system HLAE --close-game-after-recording --no-show-only-death-notices --show-assists --record-audio --concatenate-sequences --ffmpeg-video-codec h264_nvenc --ffmpeg-crf 15 --ffmpeg-output-parameters "-cq 15 -preset p7 -profile:v high -pix_fmt yuv420p -level 5.1" --cfg "C:\full\path\to\assets\cs2_pov.cfg"
```

Use `python scripts/pov/render_pov.py <demo_path> <steam_id>` instead — it wraps the above command with auto round-detection, p1/p2 split handling, and batch output naming.

All scripts pass `--cfg assets/cs2_pov.cfg` which configures HUD and restores keybinds via `exec autoexec`. The crosshair comes from CS2's `autoexec.cfg` in the game's `csgo/cfg/` directory — `render_pov.py` swaps `autoexec_render.cfg` (pro's crosshair, extracted from demo) and `autoexec_personal.cfg` (your crosshair) before/after rendering.

### Split demos (p1, p2)

HLTV sometimes splits match demos into parts. The render script auto-detects companion parts and renders them sequentially.

To manually concatenate split renders:
```powershell
ffmpeg -f concat -safe 0 -i <filelist.txt> -c copy "renders\combined.mp4"
```

### Concatenating rendered rounds

After rendering all rounds with `python scripts/pov/render_pov.py`, join them into one video:
```powershell
python scripts/pov/concat_rounds.py <renders_folder>
```
Output is `combined.mp4` in the same folder. Concat is incremental (one batch at a time with ffmpeg stream copy), then upscaled to 1440p via CUDA Lanczos. Each batch file is deleted after successful append — remaining `batch-*.mp4` files on disk indicate which batches still need to be concat'd on resume.

## Known Gotchas

- **Autoexec crosshair swap** — CS2 reads crosshair from `game/csgo/cfg/autoexec.cfg` on startup. `assets/cs2_pov.cfg` executes `exec autoexec` after keybind restore, so autoexec.cfg's crosshair takes effect. Two files live alongside it: `autoexec_render.cfg` (pro's crosshair for renders) and `autoexec_personal.cfg` (your crosshair for playing). `render_pov.py` swaps them before/after rendering. If both are missing, just rename either one to `autoexec.cfg`. `autoexec_personal_backup.cfg` is a safety copy.

- **NEVER clean up avatars** — `demos/avatars/` is a persistent cache. Avatars are reused across all matches. Never delete avatar files during cleanup.

- **HLTV demo acquisition** — Pipeline step 1 and `hltv match` use CloakBrowser (`scrapers/hltv_acquire.py`), persistent profile `.cloak-hltv-profile/`. Undersized archives (&lt;1MB) are not treated as cache hits. Fallback: pass `--demo` with a local `.rar` or `.dem`.
- **HLTV page scraping** — `fetch_hltv_page_html()` uses Chrome DevTools Protocol (CDP) auto-launch with temp profile (port 9222). Kills stale temp-profile Chrome on startup. Rate-limited retry: up to 10 attempts, delay `min(2*attempt, 60)`. System DNS respects, no profile lock conflicts. `HLTVScraper` also uses CDP `connect_over_cdp` fallback with single reusable page + per-navigation rate limiting.
- **Split demos (p1, p2)** — IEM tournaments sometimes split single-map demos into parts (`-p1`, `-p2`). `render_pov.py` handles this automatically. The `.rar` may contain multiple `.dem` files — `extract_demo()` in `downloader.py` now extracts all of them.
- **PBDEMS2 format** — PGL tournaments use a custom demo format. csdm now supports it (requires `--source challengermode` for analyze). Use `csdm analyze <demo> --source challengermode`.
- **HLTV CDN blocks image downloads** — Player body shots must be scraped via shared `HLTVScraper` (single Chrome instance, response interception). Rate-limited with 2s delay between players. One reusable page per scraper, no fresh context per player.
- **Background removal at download time** — `rembg` runs during avatar download (not during thumbnail generation). Player page images (400×417) are already transparent — rembg only needed for match page fallback (200×200 with bg). Cutout PNGs saved as `{nickname}.png` in `demos/avatars/`.
- **Thumbnail background auto-extraction** — When using `--demo` + `--steam-id`, the thumbnail generator renders a 1-second clip of a random kill, extracts the first frame, blurs it (radius 6), and uses it as background.
- **VP9 Trick** — Render at 2560×1440 even for 1080p-targeted uploads. YouTube gives 1440p+ videos VP9 codec (higher bitrate), making them sharper even when watched at 1080p.
- **YouTube encoding** — Use `--ffmpeg-output-parameters "-profile:v high -pix_fmt yuv420p -level 4.2"` for YouTube compatibility. YouTube may still take 30-60 min to process 1080p60 after upload.
- **Windows PowerShell** — does not support `&&`, `||`, `tail`. Use `; if ($?) { }` for chaining. Use `Select-String -Last 3` instead of `tail -3`. `@'...'@` heredocs broken with f-strings — write Python scripts to files instead.
- **RAR extraction** — `rarfile` library doesn't work on Windows. Use `patoolib` (via `patool` pip package) which wraps WinRAR.
- **CS2 launch for rendering** — csdm may fail to auto-launch CS2 if Steam is on a secondary drive. Just open CS2 manually to any menu before running `render_pov.py`.
- **All async** — every scraper/command is `asyncio.run()`. Any new command must follow `async def` + `register_subparser` + `handle` pattern in `commands/`.
- **YouTube scheduling** — Pipeline defaults to `--publish-at auto`: next future **10:00 or 16:30 Australia/Sydney** slot on a free day, using **YouTube API** (`get_youtube_publish_dates` queries channel uploads playlist for `status.publishAt` dates) to find open slots. Local ledger (`youtube/.publish_schedule.json`) is deprecated and no longer used for scheduling. Explicit `--publish-at "YYYY-MM-DD HH:MM"` still schedules exactly.
- **YouTube verification** — To set custom thumbnails, your YouTube account must be verified (phone verify) at https://www.youtube.com/verify.
- **Wrong HLTV match URL ID** — If the ratings scraper returns "Unknown Match" or empty tables, the match URL's numeric ID is wrong. HLTV uses SPAs where the JS routing matches the ID to the match. Check the correct ID by visiting the match page in a browser (the sidebar "Related matches" links have correct IDs).
- **Overlay pipeline** (`overlay_pov.py`) — sprite-based ffmpeg filter_complex. 18 sprite PNGs (9 keys × idle/pressed), not full-frame per-frame PNGs. Key caps at 76×76 with rounded rects, stepped release fade (12 frames, 4 steps), proper grid positioning. Reads `input_overlay.parquet` from CS2UtilArchive results when available; otherwise extracts via demoparser2 bitmask (`buttons` field → `decode_button_mask`). Generates assets via `overlay_assets.generate_key_assets()`, builds filter via `build_png_overlay_filter()`. Single ffmpeg call with `-loop 1 -i` sprite inputs.
- **Agent error parsing** — Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling. Common codes: `EXTRACT_MAP_NOT_FOUND`, `RATINGS_NO_TABLES`, `ANALYZE_NO_ROUNDS`, `RENDER_STEAM_NOT_RUNNING`, `CONCAT_FAILED`, `THUMBNAIL_MISSING`, `UPLOAD_NO_VIDEO_ID`, `HF_DOWNLOAD_FAILED`.
- **HF download failure** — Pipeline pulls single `.dem` from `cs2povarchive/cs2-demos` dataset when local file missing. Requires `hf_root` in backlog. If HF download fails (wrong root, file not uploaded), error code `HF_DOWNLOAD_FAILED`. Demo-level granularity — only the needed `.dem` is downloaded, not the full match archive.
- **HLTV Cloudflare block** — If CloakBrowser or Playwright get `net::ERR_CONNECTION_RESET` on HLTV while regular Chrome works, Cloudflare is fingerprinting the automation browser. Fix: use Playwright with system Chrome + `ignore_default_args=["--enable-automation"]` to hide automation flags. Applied in `scrapers/hltv_acquire.py` (`fetch_hltv_page_html`) and `scrapers/hltv.py` (`HLTVScraper._ensure_browser()`). Forcing custom DNS (`--dns-server`) does NOT help — DNS resolves fine, block is TCP-level from Cloudflare.

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

## Architecture

```
main.py → routing dict → commands/*.py (subparser + handle) → scrapers/*.py (async I/O)
                                                           → downloader.py (file ops)
                                                           → models.py (Pydantic)
```

- `commands/` — CLI handlers, one file per command. Each exports `register_subparser(subparsers)` and `handle(args)`.
- `scrapers/` — async I/O (HLTV, FACEIT, YouTube API). Uses Playwright for Cloudflare-bypassed scraping.
- `downloader.py` — file management, archive extraction, download history JSON.
- `models.py` — Pydantic models shared across modules.
- `thumbnail/` — thumbnail generator package (Pillow-based compositing, 1280×720 output).
- `scripts/` — utility scripts grouped by product (`pov/`, `overlay/`, `faceit/`, `highlights/`, `upload/`, `hf/`, `misc/`) plus `scripts/_pathsetup.py`
- `docs/` — design/context notes (`CONTEXT.md`, batching, dual-upload) and `docs/adr/`
- `CONTEXT.md` — domain glossary for the Highlight Reel / Kill Timeline product
- `assets/` — static resources (map images, fonts, CS2 config files).
- `grafipy-out/` — knowledge graph (from `/graphify`). Not project code.

## Skills Available

Installed from mattpocock/skills (see `skills-lock.json`):
- `/grill-me` — stress-test a plan via relentless questioning
- `/grill-with-docs` — same but also updates `docs/CONTEXT.md` + ADRs
- `/handoff` — compact session into handoff doc for next agent
- `/caveman` — ultra-compressed mode
- `/wikify` — generate a Karpathy-style wiki from a thesis, project, paper, or report. Extracts concepts, methods, papers, datasets, and architecture into interlinked Markdown wiki articles.
