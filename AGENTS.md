# AGENTS.md — CS2Archive

> **ALWAYS use `scripts/create_backlog.py` for data acquisition, then `scripts/pipeline.py` for rendering/upload.** Individual step scripts exist for debugging or resuming a failed step — see below.

## Pipeline (Primary Entry Point)

`python scripts/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--until N]`

Reads all POV metadata from the backlog file. Runs steps 1-7 in order. Resumable — state saved to `.pipeline/{run_id}.json`. Use `--step N` to start at a specific step.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | analyze | `csdm analyze <demo>` |
| 2 | render | `python scripts/render_pov.py <demo> <steam_id>` |
| 3 | concat | `python scripts/concat_rounds.py <renders_folder>` |
| **4** | **overlay (optional)** | `python scripts/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N]` |
| 5 | outro | `python scripts/generate_outro.py <video.mp4>` |
| 6 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --demo <dem> --steam-id <id>` |
| 7 | upload | `python scripts/upload_youtube.py <video> --title <t> --thumbnail <thumb.png>` |
| 8 | cleanup | `python scripts/cleanup_renders.py <renders_folder>` |

Omit step 4 to skip overlay entirely. Use `--step 4` to include it, or run pipeline up to step 3 and then manually invoke overlay.

**Overlay step (step 4) does two things:**
1. Extracts keyboard states via demoparser2 (full round, not sparse parquet)
2. Renders utility throw flight clips via CSDM `build_flight_command()` (chase camera) then composites as PiP overlays at bottom-left

Throw clips are rendered in sequence via CSDM/HLAE — this takes ~1-2 minutes per throw. For a full match with 20+ throws, budget 30-60 minutes.


### Structured Errors (agent-parseable)

Every pipeline step validates its output and exits with a single JSON error line on failure:

```
[PIPELINE_ERROR] {"error":true,"step":1,"step_name":"analyze","code":"ANALYZE_NO_ROUNDS","message":"csdm analysis has zero rounds"}
```

Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling (e.g. `RENDER_STEAM_NOT_RUNNING`, `OUTRO_CONCAT_FAILED`, `THUMBNAIL_MISSING`, `UPLOAD_NO_VIDEO_ID`).

### Example

```
python scripts/pipeline.py --backlog backlog/spirit-vs-falcons-iem-cologne-major/high/tnir-mirage-spirit-vs-falcons-iem-cologne-major.json --step 7
```

### Notes

- Pipeline reads `steam_id` from backlog entry (resolved by `create_backlog.py` from `.data/player_accounts.json`). No `--steam-id` CLI flag — source of truth is `.data/player_accounts.json` (via `python main.py player add/list`). Extract from demo: `python scripts/extract_steamids.py <demo_path>`.
- `--demo` optional in pipeline: omit to download from `hltv_url` (CloakBrowser) or from HuggingFace if `hf_root` set; pass `.rar` or `.dem` to skip download.`--force` re-downloads.
- **HF auto-download:** if `demo_path` not found locally and backlog has `hf_root` (e.g. `iem_cologne_major_2026`), pipeline pulls single `.dem` from `cs2povarchive/cs2-demos` dataset. Demo-level granularity — only the needed map is downloaded.
- Render step verifies Steam is running before starting.
- Each step validates its output before proceeding — failures halt the pipeline.
- **Resume rule: ALWAYS check `.pipeline/{run_id}.json` before deleting any saved progress (combined.mp4, rendered clips, etc.). The pipeline state tells you which step was last completed. Run `python scripts/pipeline.py --backlog <path> --step <N>` (same backlog) to resume.
- **Render folder per POV** — `renders/pov-{demo-stem}_{player}/` (not demo-only). Multiple POVs on the same map share the match demo folder but never share a render folder. Legacy `pov-{demo-stem}/` (no player suffix) may still exist from older runs; safe to delete after confirming youtube output.
- **`--resume-from-round N`** — deprecated. Render now uses filesystem-based resume: existing `batch-*.mp4` files ≥1MB are automatically skipped on re-run. To re-render a specific batch, manually delete its file.
- **`--batches N`** — rounds per render batch (default: 10). Each batch produces one MP4 named `batch-{start:03d}-{end:03d}.mp4`. `--batches 1` is equivalent to the old per-round model.
- **`--until N`** — stop after step N (e.g. `--until 5` runs through thumbnail, skips upload/cleanup). Default: run through step 7.
- **`--dual-upload`** — produce and upload a **second independent variant** with the keyboard + util-cam overlay applied. Default behavior (no flag) is 100% unchanged.
- **`--overlay-only`** — upload only the overlay variant. Implies `--dual-upload`'s overlay branch but skips raw video copy / raw outro / raw thumbnail / raw upload. No `youtube/{run_id}/` dir created, no `youtube_id` state key. Use when you only want the keyboard+util-cam version on the channel. State stored under `overlay_only=True` for resume.

### Dual-Upload (`--dual-upload`)

When passed, the pipeline produces **two** separate YouTube uploads from one backlog entry:

| Variant | YouTube dir | Title suffix | Thumbnail | Description |
|---|---|---|---|---|
| Raw | `youtube/{run_id}/` | _(none)_ | standard | standard |
| Overlay | `youtube/{run_id}_overlay/` | `\| Input Overlay + Utility Cam` | + `W/ INPUT OVERLAY` badge top-right | + overlay note paragraph |

Both variants get independent `upload_meta.json`, independent YouTube video IDs (`youtube_id` + `overlay_youtube_id` in state), and independent publish-schedule slots.

**Data flow:**
1. Step 3 (concat): `combined.mp4` copied to both `youtube/{run_id}/` and `youtube/{run_id}_overlay/`
2. Step 4 (overlay): runs `overlay_pov.py` on the overlay dir's `video.mp4`; output replaces `video.mp4` in the overlay dir. Skipped in raw-only mode (no `--dual-upload`) so cost is zero.
3. Step 5 (outro): appended to both `video.mp4` files
4. Step 6 (thumbnail): two thumbnails generated, each with its own `upload_meta.json`
5. Step 7 (upload): both uploaded sequentially, each with its own resume-safe state key
6. Step 8 (cleanup): unchanged

**Resume:** if raw uploads but overlay fails, re-running with the same `--dual-upload` flag re-uploads only the overlay variant. Each variant's `upload_meta.json` is checked for an existing `youtube_id` before re-uploading.

**Cost:** adds one full overlay rendering (~30–60 min for 20+ throws) and one extra YouTube upload. Use only for matches where the overlay version adds value (high-profile matches, educational content).

**Backwards compat:** without `--dual-upload`, behavior is **identical** to before — step 4 still runs the overlay script but its sidecar output is left orphaned exactly as before. No new directories created. No new state keys. Existing `youtube/{run_id}/` and `.pipeline/{run_id}.json` files unaffected.

**`--overlay-only`** is a strict subset of `--dual-upload` for the overlay branch. Resuming a failed overlay-only run with the same flag re-runs only the missing overlay work; no raw artifacts are ever produced.

### Chaining pipelines (upload overlap)

Use `scripts/pipeline_chain.py` to start the **next** POV when the **previous** reaches **upload** (state `step >= 6`). Only one render (step 2) should run at a time; upload (step 6) can overlap with the next POV’s acquire→render.

**How it works:** polls `.pipeline/{run_id}.json` every 30s (`--poll`). When `"step" >= 6`, spawns `pipeline.py` with the args you pass after `--`. Does **not** read terminal output — only the state file.

**`run_id`** = `{match_id}_{demo_stem}_{player}_{map}` (e.g. `2394174_falcons-vs-mouz-m3-nuke_NiKo_Nuke`). Includes HLTV match ID to prevent collision when the same teams/map/player appear in different tournaments. Use `match_id_from_url()` from `scrapers/hltv_acquire.py` to extract the ID. Same args as the watched pipeline must be used when resuming.

```powershell
# When NiKo hits upload, start kyousuke (chain exits after launch)
python scripts/pipeline_chain.py --watch falcons-vs-mouz-m3-nuke_NiKo_Nuke --no-wait -- `
  --backlog backlog/falcons-vs-mouz-cs-asia-championships-2026/high/kyousuke-dust2-falcons-vs-mouz-cs-asia-championships-2026.json

# Chain xelex after kyousuke (run in a second terminal)
python scripts/pipeline_chain.py --watch falcons-vs-mouz-m2-dust2_kyousuke_Dust2 --no-wait -- `
  --backlog backlog/falcons-vs-mouz-cs-asia-championships-2026/high/xelex-mirage-falcons-vs-mouz-cs-asia-championships-2026.json
```

- **`--no-wait`** — start the next pipeline and exit (recommended; each POV runs in its own process/terminal).
- Omit `--no-wait` to block until the spawned pipeline finishes.
- Start POV A’s pipeline first, then start chain watcher(s) in separate background terminals.

## Individual Step Scripts (Debugging / Manual Use)

Use these when debugging a specific pipeline step failure or running steps manually:

1. **Demo Download** — `python main.py hltv match <url>` (CloakBrowser, same as pipeline step 1). Archives and `.dem` files land in `demos/hltv/<match-slug>/`. Use `--force` to re-download.
2. **Ratings** — `python main.py ratings <url>` scrapes HLTV Rating 3.0 for the match (saves to `demos/analysis/`). Check top players: `python scripts/best_per_map.py demos/analysis/{slug}_ratings.json`
3. **Avatars** — `scrapers/player_images.py`. Uses shared `HLTVScraper` (single Chrome instance) across all players. Navigates **player page** to capture 400×417 transparent PNG via response interception. Falls back to match page → rembg if player page fails. Rate-limited: 2s delay between players. Saved as `{nickname}.png` in `demos/avatars/`.
4. **Player Steam ID** — `python main.py player add <nickname> --steam <url>` or `python main.py player list`. Extract from demo: `python scripts/extract_steamids.py <demo_path>`.
5. **CSDM Analysis** — `csdm analyze <demo>`. For PGL demos (PBDEMS2 format): `csdm analyze <demo> --source challengermode`.
6. **Render POV Clip** — `python scripts/render_pov.py <demo_path> <steam_id> [--batches 3]`. Extracts player's crosshair from demo, writes to `autoexec_render.cfg`, copies over `autoexec.cfg` in CS2's cfg dir (`game/csgo/cfg/`), renders rounds in batches. On exit (even crash), swaps `autoexec_personal.cfg` back. Each batch produces one `batch-{start}-{end}.mp4` file. Filesystem-based resume: existing `batch-*.mp4` files ≥1MB are automatically skipped.
7. **Concatenate Rounds** — `python scripts/concat_rounds.py <renders_folder>` → `combined.mp4` (incremental batch-by-batch concat with gap/overlap validation, then upscale to 1440p). Each batch file is deleted after successful append.
8. **Render Util-Cams (prep + render)** — `python scripts/render_util_cams.py --util-cams-root <pov>/utility_cams --data-dir <data-dir> [--steamid <id>] [--chunk-size 0]`. Two-phase: **(1) PREP** filters throws.parquet by `--steamid` (optional, default = all players) + `flight_ticks > 0` + `is_renderable=True`, creates `unnamed/<throw_id_slug>/` util_cam dir + `_throw_poses.json` per throw (one dir per throw_id, no aggregation); **(2) RENDER** discovers dirs needing render (no `.mp4`), calls CS2UtilArchive's `render_spot_batch` in batched chunks. **Cameras convention** (from manifest, set by prep): smoke → `"throw,flight"` → mp4 `throw_flight_<slug>.mp4`; others → `"flight"` → mp4 `flight_<slug>.mp4`. CS2UtilArchive's `spot_deliverable_path` hardcodes `throw_flight_victims_spot1` for flash util_type — flash renders are renamed to `flight_<slug>.mp4` post-render to match convention. `--chunk-size 0` (default) renders all spots in one CS2 launch. Idempotent — re-runs are no-ops for already-rendered clips. Flags: `--prepare-only` (just create dirs), `--render-only` (skip prep, just render existing dirs). **Run after `extract_utils.py` produces throws.parquet** — script reads it via `--data-dir`.
9. **Overlay** — `python scripts/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N]`. Applies keyboard overlay (demoparser2 full extraction) + utility throw flight PiP (CSDM flight renders at bottom-left). Requires CS2UtilArchive with throws.parquet. Renders flight clips via `build_flight_command()` — ~1-2 min per throw.
10. **Generate Thumbnail** — `python -m thumbnail <match_url> --player <nick> --map <map> --demo <dem> --steam-id <id> [--tournament "IEM Atlanta 2026"]`. Auto-extracts random kill frame as blurred background. Or `--background <frame.jpg>`.
   Example: `python -m thumbnail "https://www.hltv.org/matches/2394166/faze-vs-vitality-iem-atlanta-2026" --player ropz --map Nuke --demo demos/hltv/.../faze-vs-vitality-m1-nuke-p2.dem --steam-id 76561197991272318 --tournament "IEM Atlanta 2026"`
11. **Generate Title & Description** — `python scripts/generate_title.py <ratings_json> --player <nick> --map <map> [--tournament "..."]`. Outputs JSON with `title` and `description` from ratings data.
12. **Upload to YouTube** — `python scripts/upload_youtube.py <video_path> --thumbnail <thumb.png> --title <title> --description <desc> --privacy public`. Requires Google Cloud OAuth (`client_secret.json`). First-time auth opens browser. Account must be phone-verified for custom thumbnails.
   Default publish mode is `auto`: schedule at the next future 16:30 Australia/Sydney on a free local calendar day. The script keeps `youtube/.publish_schedule.json` as the slot ledger and rolls back a reserved slot if upload fails.
   Override with `--publish-at "YYYY-MM-DD HH:MM"` for an exact time, or keep `--publish-at auto` explicit.
   Or use `--meta <upload_meta.json>` to read title/description/tags from a metadata file. The pipeline writes `upload_meta.json` at step 5 (thumbnail), so step 6 can resume with only the youtube folder. The file also stores `resumable_uri`/`resumable_progress` during upload for crash recovery, and `youtube_id` after completion.

## Backlog Creation

`python scripts/create_backlog.py <hltv_url>` — downloads a match and generates prioritized backlog entries for every player/map combo.

**Demos are downloaded automatically.** The script calls into `acquire_match()` then scrapes HLTV Rating 3.0, creating a per-player backlog card ranked by rating. It validates that the `.dem` file for each map exists on disk — if not found, it raises `FileNotFoundError` with the expected path, rather than writing a placeholder.

Each backlog entry contains full metadata as JSON: player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority. The script also scrapes tournament name from HLTV, fetches player avatars, and adds `hf_root` for HuggingFace demo auto-download.

```powershell
# 1. Download demo + create backlog entries (all-in-one)
python scripts/create_backlog.py "https://www.hltv.org/matches/2394998/g2-vs-spirit-iem-cologne-major-2026"
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
  $env:PYTHONPATH="."; & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pipeline.py <args>
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

Use `python scripts/render_pov.py <demo_path> <steam_id>` instead — it wraps the above command with auto round-detection, p1/p2 split handling, and batch output naming.

All scripts pass `--cfg assets/cs2_pov.cfg` which configures HUD and restores keybinds via `exec autoexec`. The crosshair comes from CS2's `autoexec.cfg` in the game's `csgo/cfg/` directory — `render_pov.py` swaps `autoexec_render.cfg` (pro's crosshair, extracted from demo) and `autoexec_personal.cfg` (your crosshair) before/after rendering.

### Split demos (p1, p2)

HLTV sometimes splits match demos into parts. The render script auto-detects companion parts and renders them sequentially.

To manually concatenate split renders:
```powershell
ffmpeg -f concat -safe 0 -i <filelist.txt> -c copy "renders\combined.mp4"
```

### Concatenating rendered rounds

After rendering all rounds with `python scripts/render_pov.py`, join them into one video:
```powershell
python scripts/concat_rounds.py <renders_folder>
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
- **YouTube scheduling** — Pipeline defaults to `--publish-at auto`: next future 16:30 Australia/Sydney slot on a free local calendar day, using local `youtube/.publish_schedule.json` to avoid same-day duplicates. Explicit `--publish-at "YYYY-MM-DD HH:MM"` still schedules exactly.
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
- `scripts/` — utility scripts for the pipeline (render, concat, cleanup, upload, `pipeline_chain.py`).
- `assets/` — static resources (map images, fonts, CS2 config files).
- `grafipy-out/` — knowledge graph (from `/graphify`). Not project code.

## Skills Available

Installed from mattpocock/skills (see `skills-lock.json`):
- `/grill-me` — stress-test a plan via relentless questioning
- `/grill-with-docs` — same but also updates CONTEXT.md + ADRs
- `/handoff` — compact session into handoff doc for next agent
- `/caveman` — ultra-compressed mode
- `/wikify` — generate a Karpathy-style wiki from a thesis, project, paper, or report. Extracts concepts, methods, papers, datasets, and architecture into interlinked Markdown wiki articles.
