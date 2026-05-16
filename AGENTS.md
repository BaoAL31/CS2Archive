# AGENTS.md — CS2Archive

> **ALWAYS use `scripts/pipeline.py` as the primary entry point.** Individual step scripts exist for debugging or resuming a failed step — see below.

## Pipeline (Primary Entry Point)

`python scripts/pipeline.py <player> <map> <hltv_url> --steam-id <id> --demo <dem_path> [--tournament "IEM Atlanta 2026"] [--step N] [--privacy public]`

Runs steps 1-10 in order. Resumable — state saved to `.pipeline_{run_id}.json`. Use `--step N` to start at a specific step.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | extract | `patoolib` wraps WinRAR in `downloader.py` |
| 2 | ratings | `python main.py ratings <url>` → `demos/analysis/` |
| 3 | steam_id | `python main.py player add <nick> --steam <id>` |
| 4 | avatar | `scrapers/player_images.py` → `demos/avatars/{nick}.png` |
| 5 | analyze | `csdm analyze <demo>` |
| 6 | render | `python scripts/render_pov.py <demo> <steam_id>` |
| 7 | concat | `python scripts/concat_rounds.py <renders_folder>` |
| 8 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --demo <dem> --steam-id <id>` |
| 9 | upload | `python scripts/upload_youtube.py <video> --title <t> --thumbnail <thumb.png>` |
| 10 | cleanup | `python scripts/cleanup_renders.py <renders_folder>` |

### Structured Errors (agent-parseable)

Every pipeline step validates its output and exits with a single JSON error line on failure:

```
[PIPELINE_ERROR] {"error":true,"step":5,"step_name":"analyze","code":"ANALYZE_NO_ROUNDS","message":"csdm analysis has zero rounds"}
```

Grep for `[PIPELINE_ERROR]` and parse the JSON. Each error has a unique `code` for programmatic handling (e.g. `RENDER_STEAM_NOT_RUNNING`, `THUMBNAIL_MISSING`, `UPLOAD_NO_VIDEO_ID`).

### Example

```
python scripts/pipeline.py w0nderful Anubis "https://www.hltv.org/matches/2394174/natus-vincere-vs-vitality-iem-atlanta-2026" --steam-id 76561199063068840 --demo demos/hltv/natus-vincere-vs-vitality-m2-anubis.dem --tournament "IEM Atlanta 2026" --step 7
```

### Notes

- Pipeline requires `--steam-id` (no auto-extraction).
- `--demo` accepts `.dem` or `.rar` (auto-extracts matching map).
- Render step verifies Steam is running before starting.
- Each step validates its output before proceeding — failures halt the pipeline.

## Individual Step Scripts (Debugging / Manual Use)

Use these when debugging a specific pipeline step failure or running steps manually:

1. **Demo Download** — `python main.py hltv match <url>` | If 403'd, manually download `.rar` from browser, cut from Downloads → `demos/hltv/` → extract with WinRAR (`patoolib` wraps WinRAR in `downloader.py`).
2. **Ratings** — `python main.py ratings <url>` scrapes HLTV Rating 3.0 for the match (saves to `demos/analysis/`). Check top players: `python scripts/best_per_map.py demos/analysis/{slug}_ratings.json`
3. **Avatars** — `scrapers/player_images.py`. Uses Playwright + `rembg` — **must run from browser context** (CDN requires page session). Body shots with `bg` URL param need rembg for background removal. Saved as `{nickname}.png` in `demos/avatars/`.
4. **Player Steam ID** — `python main.py player add <nickname> --steam <url>` or `python main.py player list`. Extract from demo: `python scripts/extract_steamids.py <demo_path>`.
5. **CSDM Analysis** — `csdm analyze <demo>`. For PGL demos (PBDEMS2 format): `csdm analyze <demo> --source challengermode`.
6. **Render POV Clip** — `python scripts/render_pov.py <demo_path> <steam_id>`. Renders each round as separate clip. Resume from specific round: `--rounds 10-18`. For kill compilations: `csdm video "<demo_path>" --mode player --steamids <id> --event kills ...`
7. **Concatenate Rounds** — `python scripts/concat_rounds.py <renders_folder>` → `combined.mp4` (ffmpeg stream copy, lossless).
8. **Generate Thumbnail** — `python -m thumbnail <match_url> --player <nick> --map <map> --demo <dem> --steam-id <id> [--tournament "IEM Atlanta 2026"]`. Auto-extracts random kill frame as blurred background. Or `--background <frame.jpg>`.
   Example: `python -m thumbnail "https://www.hltv.org/matches/2394166/faze-vs-vitality-iem-atlanta-2026" --player ropz --map Nuke --demo demos/hltv/.../faze-vs-vitality-m1-nuke-p2.dem --steam-id 76561197991272318 --tournament "IEM Atlanta 2026"`
9. **Generate Title & Description** — `python scripts/generate_title.py <ratings_json> --player <nick> --map <map> [--tournament "..."]`. Outputs JSON with `title` and `description` from ratings data.
10. **Upload to YouTube** — `python scripts/upload_youtube.py <video_path> --thumbnail <thumb.png> --title <title> --description <desc> --privacy public`. Requires Google Cloud OAuth (`client_secret.json`). First-time auth opens browser. Account must be phone-verified for custom thumbnails.

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

Basic tick-range render:
```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" <start_tick> <end_tick> --output "demos\renders" --framerate 60 --width 1920 --height 1080 --recording-system CS --close-game-after-recording --ffmpeg-video-container mp4 --ffmpeg-video-codec h264_nvenc --ffmpeg-crf 0 --ffmpeg-output-parameters "-profile:v high -pix_fmt yuv420p -level 4.2 -b:v 50M -maxrate 50M -bufsize 100M"
```

Tick ≈ 1/64 sec (most CS2 servers). For a 5-second clip use ~320 ticks.

### Rounds-Only POV (full HUD, no x-ray, one round at a time)

For rendering a player's POV with full HUD (radar, health, ammo) and no x-ray, one round at a time to avoid disk I/O saturation:

```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" --mode player --steamids <steam64_id> --event rounds --rounds <N> --perspective player --no-show-x-ray --output "demos\renders\pov-folder" --framerate 60 --width 1920 --height 1080 --recording-system CS --close-game-after-recording --no-show-only-death-notices --show-assists --record-audio --concatenate-sequences --ffmpeg-video-codec h264_nvenc --ffmpeg-crf 0 --ffmpeg-output-parameters "-profile:v high -pix_fmt yuv420p -level 4.2 -b:v 50M -maxrate 50M -bufsize 100M" --cfg assets/cs2_pov.cfg
```

Use `python scripts/render_pov.py <demo_path> <steam_id>` instead — it wraps the above command with auto round-detection, p1/p2 split handling, and per-round output naming.

All scripts pass `--cfg assets/cs2_pov.cfg` which disables in-game chat and configures the HUD.

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

- **Demo downloads from HLTV fail with 403** — HLTV CDN issues one-time signed URLs consumed after first download. IP gets blocked after repeated requests. Download manually from browser, cut `.rar` from Downloads → `demos/hltv/` → extract with WinRAR (`patoolib` wraps WinRAR in `downloader.py`).
- **Split demos (p1, p2)** — IEM tournaments sometimes split single-map demos into parts (`-p1`, `-p2`). `render_pov.py` handles this automatically. The `.rar` may contain multiple `.dem` files — `extract_demo()` in `downloader.py` now extracts all of them.
- **PBDEMS2 format** — PGL tournaments use a custom demo format. csdm now supports it (requires `--source challengermode` for analyze). Use `csdm analyze <demo> --source challengermode`.
- **HLTV CDN blocks image downloads** — Player body shots must be scraped via Playwright browser context (not httpx). Use `scrapers/player_images.py`.
- **Background removal at download time** — `rembg` runs during avatar download (step 4), not during thumbnail generation. Cutout PNGs are saved as `{nickname}.png` in `demos/avatars/`.
- **Thumbnail background auto-extraction** — When using `--demo` + `--steam-id`, the thumbnail generator renders a 1-second clip of a random kill, extracts the first frame, blurs it (radius 6), and uses it as background.
- **Quality settings** — All renders use `h264_nvenc` with `-b:v 50M -maxrate 50M -cq 0` (max bitrate, best quality) at 1080p60. libx264 was used before but used more CPU.
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
    ├── thumbnail.png    (1280×720 PNG, auto-generated)
    └── video.mp4        (1080p60, full match POV, concatenated rounds)
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
- `scripts/` — utility scripts for the pipeline (render, concat, cleanup, upload).
- `assets/` — static resources (map images, fonts, CS2 config files).
- `grafipy-out/` — knowledge graph (from `/graphify`). Not project code.

## Skills Available

Installed from mattpocock/skills (see `skills-lock.json`):
- `/grill-me` — stress-test a plan via relentless questioning
- `/grill-with-docs` — same but also updates CONTEXT.md + ADRs
- `/handoff` — compact session into handoff doc for next agent
- `/caveman` — ultra-compressed mode
