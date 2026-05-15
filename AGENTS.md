# AGENTS.md — CS2Archive

## Pipeline (YouTube Thumbnail Workflow)

> **HLTV only.** FACEIT workflow may differ — not documented yet.

1. **`python main.py trending`** — Find top trending CS2 matches from YouTube highlight channels (matched to HLTV). Pick the match URL.
2. **Demo Download** — `python main.py hltv match <url>` downloads the demo. If 403'd, manually download `.rar` from browser, then cut from Downloads → `demos/hltv/` → extract with WinRAR.
3. **Ratings** — `python main.py ratings <url>` scrapes HLTV Rating 3.0 for the match (saves to `demos/analysis/`).
    - Check top players: `python scripts/best_per_map.py demos/analysis/{slug}_ratings.json`
4. **Avatars** — Avatars are auto-downloaded by `test-pipeline` or manually via `scrapers/player_images.py`. Background is automatically removed and saved as `{nickname}.png`. Check `demos/avatars/`.
5. **Player Steam ID** — `python main.py player add <nickname> --steam <url>` or check existing with `python main.py player list`. If you have the demo file, you can extract Steam IDs directly with `python scripts/extract_steamids.py <demo_path>`.
6. **CSDM Analysis** — `csdm analyze` the demo so video rendering can find events.
7. **Render POV Clip** — `python scripts/render_pov.py <demo_path> <steam_id>` renders each round as a separate clip (full HUD, no x-ray). To resume from a specific round: `--rounds 10-18`. For kill compilations instead of rounds, use `csdm video "<demo_path>" --mode player --steamids <id> --event kills --start-seconds-before 2 --end-seconds-after 2 --output "demos/renders"`.
8. **Concatenate Rounds** — `python scripts/concat_rounds.py <renders_folder>` joins all round-NNN clips into `combined.mp4`.
9. **Move to YouTube** — Place `combined.mp4` (renamed `video.mp4`) and the thumbnail into `youtube/{match-slug}_{player}_{map}/`.
10. **Cleanup Renders** — `python scripts/cleanup_renders.py <renders_folder> --youtube <youtube_folder>` removes the entire renders folder after confirming the video is in youtube/.
11. **Generate Thumbnail** — `python -m thumbnail <match_url> --player <nickname> --map <mapname> --demo <demo_path> --steam-id <id> [--tournament "IEM Atlanta 2026"]`
    Auto-extracts a random kill frame as blurred background. Or use `--background <frame.jpg>` to specify manually.
    Example: `python -m thumbnail "https://www.hltv.org/matches/2394166/faze-vs-vitality-iem-atlanta-2026" --player ropz --map Nuke --demo demos/hltv/.../faze-vs-vitality-m1-nuke-p2.dem --steam-id 76561197991272318 --tournament "IEM Atlanta 2026"`
12. **Upload to YouTube** — `python scripts/upload_youtube.py <video_path> --thumbnail <thumbnail.png> --title <title> --description <desc> --privacy public`

    Requires a Google Cloud project with YouTube Data API v3 enabled and OAuth 2.0 desktop credentials
    (`client_secret.json` in project root). First-time auth opens a browser for Google login. Your YouTube account
    must be verified (phone verify) to set custom thumbnails.

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
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" <start_tick> <end_tick> --output "demos\renders" --framerate 60 --width 1920 --height 1080 --recording-system CS --close-game-after-recording --ffmpeg-video-container mp4 --ffmpeg-video-codec libx264
```

Tick ≈ 1/64 sec (most CS2 servers). For a 5-second clip use ~320 ticks.

### Rounds-Only POV (full HUD, no x-ray, one round at a time)

For rendering a player's POV with full HUD (radar, health, ammo) and no x-ray, one round at a time to avoid disk I/O saturation:

```powershell
& "C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd" video "<demo_path>" --mode player --steamids <steam64_id> --event rounds --rounds <N> --perspective player --no-show-x-ray --output "demos\renders\pov-folder" --framerate 30 --width 1920 --height 1080 --recording-system CS --close-game-after-recording --no-show-only-death-notices --show-assists --record-audio --concatenate-sequences --ffmpeg-video-codec libx264 --ffmpeg-crf 18
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
- **PBDEMS2 format** — PGL tournaments use a custom demo format (`PBDEMS2` header instead of `HL2DEMO`). Not parseable by CS2 Demo Manager or awpy. Only watchable via `playdemo` in CS2.
- **HLTV CDN blocks image downloads** — Player body shots must be scraped via Playwright browser context (not httpx). Use `scrapers/player_images.py`.
- **Background removal at download time** — `rembg` runs during avatar download (step 4), not during thumbnail generation. Cutout PNGs are saved as `{nickname}.png` in `demos/avatars/`.
- **Thumbnail background auto-extraction** — When using `--demo` + `--steam-id`, the thumbnail generator renders a 1-second clip of a random kill, extracts the first frame, blurs it (radius 6), and uses it as background.
- **Quality settings** — All renders use `libx264` with `--ffmpeg-crf 18` (high quality) at 1080p60. NVENC was tried but produced worse quality.
- **Windows PowerShell** — does not support `&&`, `||`, `tail`. Use `; if ($?) { }` for chaining. Use `Select-String -Last 3` instead of `tail -3`. `@'...'@` heredocs broken with f-strings — write Python scripts to files instead.
- **RAR extraction** — `rarfile` library doesn't work on Windows. Use `patoolib` (via `patool` pip package) which wraps WinRAR.
- **All async** — every scraper/command is `asyncio.run()`. Any new command must follow `async def` + `register_subparser` + `handle` pattern in `commands/`.
- **YouTube verification** — To set custom thumbnails, your YouTube account must be verified (phone verify) at https://www.youtube.com/verify.

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
