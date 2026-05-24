# AGENTS.md — CS2Archive

> **ALWAYS use `scripts/pipeline.py` as the primary entry point.** Individual step scripts exist for debugging or resuming a failed step — see below.

## Pipeline (Primary Entry Point)

`python scripts/pipeline.py <player> <map> <hltv_url> --steam-id <id> [--demo <dem_path>] [--tournament "IEM Atlanta 2026"] [--step N] [--until N] [--resume-from-round N] [--privacy public]`

Runs steps 1-11 in order. Resumable — state saved to `.pipeline/{run_id}.json`. Use `--step N` to start at a specific step.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | acquire | CloakBrowser download from HLTV URL → `extract_demo()` in `downloader.py` |
| 2 | ratings | `python main.py ratings <url>` → `demos/analysis/` |
| 3 | steam_id | `python main.py player add <nick> --steam <id>` |
| 4 | avatar | `scrapers/player_images.py` → `demos/avatars/{nick}.png` |
| 5 | analyze | `csdm analyze <demo>` |
| 6 | render | `python scripts/render_pov.py <demo> <steam_id>` |
| 7 | concat | `python scripts/concat_rounds.py <renders_folder>` |
| 8 | outro | `python scripts/generate_outro.py <video.mp4>` |
| 9 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --demo <dem> --steam-id <id>` |
| 10 | upload | `python scripts/upload_youtube.py <video> --title <t> --thumbnail <thumb.png>` |
| 11 | cleanup | `python scripts/cleanup_renders.py <renders_folder>` |

### Structured Errors (agent-parseable)

Every pipeline step validates its output and exits with a single JSON error line on failure:

```
[PIPELINE_ERROR] {"error":true,"step":5,"step_name":"analyze","code":"ANALYZE_NO_ROUNDS","message":"csdm analysis has zero rounds"}
```

Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling (e.g. `RENDER_STEAM_NOT_RUNNING`, `OUTRO_CONCAT_FAILED`, `THUMBNAIL_MISSING`, `UPLOAD_NO_VIDEO_ID`).

### Example

```
python scripts/pipeline.py w0nderful Anubis "https://www.hltv.org/matches/2394174/natus-vincere-vs-vitality-iem-atlanta-2026" --steam-id 76561199063068840 --demo demos/hltv/natus-vincere-vs-vitality-m2-anubis.dem --tournament "IEM Atlanta 2026" --step 7
```

### Notes

- Pipeline requires `--steam-id` (no auto-extraction).
- `--demo` optional: omit to download from `hltv_url` via CloakBrowser; pass `.rar` or `.dem` to skip download. `--force` re-downloads; default browser is headed + humanize (`--headless` to opt out).
- Render step verifies Steam is running before starting.
- Each step validates its output before proceeding — failures halt the pipeline.
- **Resume rule: ALWAYS check `.pipeline/{run_id}.json` before deleting any saved progress (combined.mp4, rendered clips, etc.). The pipeline state tells you which step was last completed. Run `python scripts/pipeline.py <args> --step <N>` (with the SAME args as the original) to resume from that step.
- **Render folder per POV** — `demos/renders/pov-{demo-stem}_{player}/` (not demo-only). Multiple POVs on the same map share the match demo folder but never share a render folder. Legacy `pov-{demo-stem}/` (no player suffix) may still exist from older runs; safe to delete after confirming youtube output.
- **`--resume-from-round N`** (step 6 only) — passed through to `render_pov.py` to continue a partial render without re-recording earlier rounds.
- **`--until N`** — stop after step N (e.g. `--until 9` runs through thumbnail, skips upload/cleanup). Default: run through step 11.

### Chaining pipelines (upload overlap)

Use `scripts/pipeline_chain.py` to start the **next** POV when the **previous** reaches **upload** (state `step >= 10`). Only one render (step 6) should run at a time; upload (step 10) can overlap with the next POV’s acquire→render.

**How it works:** polls `.pipeline/{run_id}.json` every 30s (`--poll`). When `"step" >= 10`, spawns `pipeline.py` with the args you pass after `--`. Does **not** read terminal output — only the state file.

**`run_id`** = `{demo_stem}_{player}_{map}` (e.g. `falcons-vs-mouz-m3-nuke_NiKo_Nuke`). Same args as the watched pipeline must be used when resuming.

```powershell
# When NiKo hits upload, start kyousuke (chain exits after launch)
python scripts/pipeline_chain.py --watch falcons-vs-mouz-m3-nuke_NiKo_Nuke --no-wait -- `
  kyousuke Dust2 "https://www.hltv.org/matches/2394225/falcons-vs-mouz-cs-asia-championships-2026" `
  --steam-id 76561199032006224 `
  --demo "demos/hltv/falcons-vs-mouz/falcons-vs-mouz-m2-dust2.dem" `
  --tournament "CS Asia Championships 2026"

# Chain xelex after kyousuke (run in a second terminal)
python scripts/pipeline_chain.py --watch falcons-vs-mouz-m2-dust2_kyousuke_Dust2 --no-wait -- `
  xelex Mirage "https://www.hltv.org/matches/2394225/falcons-vs-mouz-cs-asia-championships-2026" `
  --steam-id 76561198998266210 `
  --demo "demos/hltv/falcons-vs-mouz/falcons-vs-mouz-m1-mirage.dem" `
  --tournament "CS Asia Championships 2026"
```

- **`--no-wait`** — start the next pipeline and exit (recommended; each POV runs in its own process/terminal).
- Omit `--no-wait` to block until the spawned pipeline finishes.
- Start POV A’s pipeline first, then start chain watcher(s) in separate background terminals.

## Individual Step Scripts (Debugging / Manual Use)

Use these when debugging a specific pipeline step failure or running steps manually:

1. **Demo Download** — `python main.py hltv match <url>` (CloakBrowser, same as pipeline step 1). Archives and `.dem` files land in `demos/hltv/<match-slug>/`. Use `--force` to re-download.
2. **Ratings** — `python main.py ratings <url>` scrapes HLTV Rating 3.0 for the match (saves to `demos/analysis/`). Check top players: `python scripts/best_per_map.py demos/analysis/{slug}_ratings.json`
3. **Avatars** — `scrapers/player_images.py`. Uses Playwright + `rembg`. Navigates **player page** (via `img.player-photo` → `closest('a')` → `href`) to capture 400×417 transparent PNG via response interception. Each player gets a **fresh BrowserContext** (`Cache-Control: no-cache`) to prevent stale cache from suppressing `response` events. Falls back to match page → rembg if player page fails. Saved as `{nickname}.png` in `demos/avatars/`.
4. **Player Steam ID** — `python main.py player add <nickname> --steam <url>` or `python main.py player list`. Extract from demo: `python scripts/extract_steamids.py <demo_path>`.
5. **CSDM Analysis** — `csdm analyze <demo>`. For PGL demos (PBDEMS2 format): `csdm analyze <demo> --source challengermode`.
6. **Render POV Clip** — `python scripts/render_pov.py <demo_path> <steam_id>`. Renders each round as separate clip. Resume from specific round: `--rounds 10-18`. For kill compilations: `csdm video "<demo_path>" --mode player --steamids <id> --event kills ...`
7. **Concatenate Rounds** — `python scripts/concat_rounds.py <renders_folder>` → `combined.mp4` (ffmpeg stream copy, lossless).
8. **Generate Thumbnail** — `python -m thumbnail <match_url> --player <nick> --map <map> --demo <dem> --steam-id <id> [--tournament "IEM Atlanta 2026"]`. Auto-extracts random kill frame as blurred background. Or `--background <frame.jpg>`.
   Example: `python -m thumbnail "https://www.hltv.org/matches/2394166/faze-vs-vitality-iem-atlanta-2026" --player ropz --map Nuke --demo demos/hltv/.../faze-vs-vitality-m1-nuke-p2.dem --steam-id 76561197991272318 --tournament "IEM Atlanta 2026"`
9. **Generate Title & Description** — `python scripts/generate_title.py <ratings_json> --player <nick> --map <map> [--tournament "..."]`. Outputs JSON with `title` and `description` from ratings data.
9. **Upload to YouTube** — `python scripts/upload_youtube.py <video_path> --thumbnail <thumb.png> --title <title> --description <desc> --privacy public`. Requires Google Cloud OAuth (`client_secret.json`). First-time auth opens browser. Account must be phone-verified for custom thumbnails.
   Or use `--meta <upload_meta.json>` to read title/description/tags from a metadata file. The pipeline writes `upload_meta.json` at step 8 (thumbnail), so step 9 can resume with only the youtube folder. The file also stores `resumable_uri`/`resumable_progress` during upload for crash recovery, and `youtube_id` after completion.

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

## Demo Video Rendering

Uses **CS2 Demo Manager (csdm)** CLI to render POV videos. Installed at:
```
C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd
```

### Recording Mode

Uses `--recording-system CS` — csdm captures frames directly to video via FFmpeg. **No intermediate TGA/PNG files are written to disk.** Previous HLAE approach broke after CS2 updates (produced "Raw files not found" errors).

### HLAE Status Check (render step pre-check)

**Before every render step (step 6), check if HLAE is fixed:**

1. Fetch the latest HLAE release tag: `https://api.github.com/repos/advancedfx/advancedfx/releases/latest` → parse `tag_name`
2. Compare with installed version `2.190.0` at `C:\Program Files (x86)\HLAE\HLAE.exe`
3. If newer: test `--recording-system HLAE` on a 1-round render. If it works, update this doc, revert `render_pov.py` to `HLAE`, and remove this check.
4. If same: stay on CS, no action needed.

Purpose: CS2 updates (especially engine/tool string changes) silently break AfxHookSource2.dll. Without this check, render produces zero frames silently.

### VP9 Trick (sharper YouTube uploads)

Render at **2560×1440** even for 1080p-targeted uploads. YouTube allocates VP9 codec (higher bitrate) to 1440p+ uploads, while 1080p gets H.264. Video looks sharper even when watched at 1080p because YouTube uses better encoding.

All scripts default to 2560×1440, 60 fps, libx264 CRF 18.

### Rounds-Only POV (full HUD, no x-ray, one round at a time)

For rendering a player's POV with full HUD (radar, health, ammo) and no x-ray, one round at a time to avoid disk I/O saturation:

```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" --mode player --steamids <steam64_id> --event rounds --rounds <N> --perspective player --no-show-x-ray --output "demos\renders\pov-folder" --framerate 60 --width 2560 --height 1440 --recording-system CS --close-game-after-recording --no-show-only-death-notices --show-assists --record-audio --concatenate-sequences --ffmpeg-video-codec libx264 --ffmpeg-crf 18 --ffmpeg-output-parameters "-profile:v high -pix_fmt yuv420p -level 4.2" --cfg assets/cs2_pov.cfg
```

Use `python scripts/render_pov.py <demo_path> <steam_id>` instead — it wraps the above command with auto round-detection, p1/p2 split handling, and per-round output naming.

All scripts pass `--cfg assets/cs2_pov.cfg` which configures HUD and restores keybinds via `exec autoexec`.

### Split demos (p1, p2)

HLTV sometimes splits match demos into parts. The render script auto-detects companion parts and renders them sequentially.

To manually concatenate split renders:
```powershell
ffmpeg -f concat -safe 0 -i <filelist.txt> -c copy "demos\renders\combined.mp4"
```

### Concatenating rendered rounds

After rendering all rounds with `python scripts/render_pov.py`, join them into one video:
```powershell
python scripts/concat_rounds.py <renders_folder>
```
Output is `combined.mp4` in the same folder. Uses ffmpeg stream copy (no re-encode, lossless).

## Known Gotchas

- **NEVER clean up avatars** — `demos/avatars/` is a persistent cache. Avatars are reused across all matches. Never delete avatar files during cleanup.

- **HLTV demo acquisition** — Pipeline step 1 and `hltv match` use CloakBrowser (`scrapers/hltv_acquire.py`), persistent profile `.cloak-hltv-profile/`. Undersized archives (&lt;1MB) are not treated as cache hits. Fallback: pass `--demo` with a local `.rar` or `.dem`.
- **Split demos (p1, p2)** — IEM tournaments sometimes split single-map demos into parts (`-p1`, `-p2`). `render_pov.py` handles this automatically. The `.rar` may contain multiple `.dem` files — `extract_demo()` in `downloader.py` now extracts all of them.
- **PBDEMS2 format** — PGL tournaments use a custom demo format. csdm now supports it (requires `--source challengermode` for analyze). Use `csdm analyze <demo> --source challengermode`.
- **HLTV CDN blocks image downloads** — Player body shots must be scraped via Playwright browser context (not httpx). Each player gets a **fresh BrowserContext** (with `Cache-Control: no-cache`) to prevent browser cache from suppressing `response` events. Use `scrapers/player_images.py`.
- **Background removal at download time** — `rembg` runs during avatar download (step 4), not during thumbnail generation. Player page images (400×417) are already transparent — rembg only needed for match page fallback (200×200 with bg). Cutout PNGs saved as `{nickname}.png` in `demos/avatars/`.
- **Thumbnail background auto-extraction** — When using `--demo` + `--steam-id`, the thumbnail generator renders a 1-second clip of a random kill, extracts the first frame, blurs it (radius 6), and uses it as background.
- **VP9 Trick** — Render at 2560×1440 even for 1080p-targeted uploads. YouTube gives 1440p+ videos VP9 codec (higher bitrate), making them sharper even when watched at 1080p.
- **YouTube encoding** — Use `--ffmpeg-output-parameters "-profile:v high -pix_fmt yuv420p -level 4.2"` for YouTube compatibility. YouTube may still take 30-60 min to process 1080p60 after upload.
- **Windows PowerShell** — does not support `&&`, `||`, `tail`. Use `; if ($?) { }` for chaining. Use `Select-String -Last 3` instead of `tail -3`. `@'...'@` heredocs broken with f-strings — write Python scripts to files instead.
- **RAR extraction** — `rarfile` library doesn't work on Windows. Use `patoolib` (via `patool` pip package) which wraps WinRAR.
- **CS2 launch for rendering** — csdm may fail to auto-launch CS2 if Steam is on a secondary drive. Just open CS2 manually to any menu before running `render_pov.py`.
- **All async** — every scraper/command is `asyncio.run()`. Any new command must follow `async def` + `register_subparser` + `handle` pattern in `commands/`.
- **YouTube verification** — To set custom thumbnails, your YouTube account must be verified (phone verify) at https://www.youtube.com/verify.
- **Wrong HLTV match URL ID** — If the ratings scraper returns "Unknown Match" or empty tables, the match URL's numeric ID is wrong. HLTV uses SPAs where the JS routing matches the ID to the match. Check the correct ID by visiting the match page in a browser (the sidebar "Related matches" links have correct IDs).
- **Agent error parsing** — Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling. Common codes: `EXTRACT_MAP_NOT_FOUND`, `RATINGS_NO_TABLES`, `ANALYZE_NO_ROUNDS`, `RENDER_STEAM_NOT_RUNNING`, `CONCAT_ROUND_MISMATCH`, `THUMBNAIL_MISSING`, `UPLOAD_NO_VIDEO_ID`.

## Output Directory Structure

After completing the pipeline for a POV:
```
youtube/
└── {match-slug}_{player}_{map}/
    ├── thumbnail.png       (1280×720 PNG, auto-generated)
    ├── video.mp4           (1080p60, full match POV, concatenated rounds)
    └── upload_meta.json    (title, description, tags, upload status, youtube_id)
```

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
