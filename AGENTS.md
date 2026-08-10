# AGENTS.md — CS2Archive

> **ALWAYS use `scripts/pov/create_backlog.py` for data acquisition, then `scripts/pov/pipeline.py` for rendering, then `scripts/upload/upload_pending.py` to upload.** Individual step scripts exist for debugging or resuming a failed step — see below.

## Reference Docs (read on demand)

Operational deep-dives live in `docs/agents/`; this file is the quick reference:

| Doc | Contents |
|---|---|
| `docs/agents/pipeline.md` | Pipeline flags, resume, structured errors, dual-upload, chaining, backlog creation, output layout |
| `docs/agents/steps.md` | Individual step scripts (debugging / manual use) |
| `docs/agents/rendering.md` | CSDM/HLAE/ffmpeg rendering details |
| `docs/agents/gotchas.md` | Full gotcha list |
| `docs/agents/shorts-titles.md` | YouTube Short title conventions + approved examples (creative, ELO/level-10 opponent labels) |

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

Separate product from POV Archive. v1 flow: action timeline → LLM edit timeline → CSDM segment renders → single reel (per-segment avatar cut-ins + crossfades).

```powershell
python scripts/highlights/build_action_timeline.py demos/faceit/<demo>.dem
# -> renders/hl-{demo_stem}/action_timeline.json
python scripts/highlights/build_edit_timeline.py demos/faceit/<demo>.dem
# -> renders/hl-{demo_stem}/edit_timeline.json  (LLM-batched, post-shaped by _fix_edit_timeline)
python scripts/highlights/render_edit_timeline.py renders/hl-<stem>/edit_timeline.json
# -> renders/hl-{stem}/segments/seg-NNN-pov-<sid>-tick-<a>-to-<b>.mp4  (batch-config CSDM, 1920x1080@64, resume-safe)
python scripts/highlights/assemble_reel.py renders/hl-<stem>/edit_timeline.json
# -> renders/hl-{stem}/reel.mp4
```

- Hard-refuses demos outside `demos/faceit/` (action timeline)
- Every kill where **at least one** of attacker or victim is a Recognised Pro (`.data/player_accounts.json`). Includes unknown→pro picks.
- Recognised Pros = `player_accounts.json` only (no `faceit_pros.json`)
- `render_edit_timeline.py` renders via CSDM `--config-file` batches; segment files are the resume unit (existing ≥1MB skipped).
- `assemble_reel.py` normalizes segments to 60fps, bakes each segment's POV player avatar (transparent cutout + white outline, bottom-centre — same treatment as `render_shorts.py`), then concatenates with `xfade`/`acrossfade` (default 0.4s) into `reel.mp4`. Intermediates in `reel_tmp/` (resumable).

## Pipeline (Primary Entry Point)

`python scripts/pov/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--until N]`

Runs steps 1-6 (analyze → render → concat → overlay → outro → thumbnail) from the backlog entry, writes `upload_meta.json` per variant. **Does NOT upload** — run `scripts/upload/upload_pending.py` afterward. Resumable via `.pipeline/{run_id}.json` (`--step N` resumes). Full details: `docs/agents/pipeline.md`.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | analyze | `csdm analyze <demo>` |
| 2 | render | `python scripts/pov/render_pov.py <demo> <steam_id>` |
| 3 | concat | `python scripts/pov/concat_rounds.py <renders_folder>` |
| **4** | **overlay** | `python scripts/overlay/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N]` |
| 5 | outro | `python scripts/pov/generate_outro.py <video.mp4>` |
| 6 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --demo <dem> --steam-id <id>` |
| 7 | cleanup | `python scripts/pov/cleanup_renders.py <renders_folder>` |

Key rules:

- **Resume rule:** ALWAYS check `.pipeline/{run_id}.json` before deleting saved progress (combined.mp4, rendered clips, …) — it records the last completed step. Resume with the same backlog + `--step N`.
- **Uploading is separate.** Pipeline stops at step 6 with `upload_status="pending"`; `upload_pending.py` uploads every pending `youtube/*/upload_meta.json` (resume-safe: skips `completed`).
- **Overlay (step 4) runs by default** (overlay-only is the default for all POVs): keyboard states via demoparser2 + utility throw flight PiP clips (~1–2 min/throw via CSDM/HLAE; 20+ throws ≈ 30–60 min). Skip with `--until 3` or `--raw-only`; `--overlay-only` is the (now default) explicit form.
- **Input overlay source:** recent FACEIT demos store usercmds in `CMsgServerUserCmd.delta_data`, which demoparser2 0.41.x misaligns (upstream PR #343 unmerged). The overlay therefore reads correct per-tick WASD/attack via `scripts/overlay/usercmd_extract.py`, which runs the vendored-parser Rust CLI at `tools/button_extract` (built from unicbm/demotracer's patched demoparser). Build it once: `cargo build --release --manifest-path tools/button_extract/Cargo.toml`.
- **`--skip-failed-rounds` — [DANGER] NEVER set by default.** Only for corrupted/incompatible demos (e.g. `100-thieves-vs-spirit-m3-dust2.dem` — fails round 1 with "Game error" for every player). Silently drops failed rounds → incomplete POV. Enabled per-invocation or via backlog `pipeline_cmd` / `skip_failed_rounds: true`.
- **Demo download:** omit `--demo` to download from `hltv_url` (CloakBrowser), or from HuggingFace if backlog has `hf_root` (single `.dem` from `cs2povarchive/cs2-demos`); pass `.rar`/`.dem` to skip; `--force` re-downloads. HF pull failure → `HF_DOWNLOAD_FAILED`.
- **Steam ID** comes from the backlog entry, resolved by `create_backlog.py` from `.data/player_accounts.json` (via `main.py player add/list`) — no `--steam-id` CLI flag. Extract from demo: `scripts/pov/extract_steamids.py <demo_path>`.
- **Render folder per POV:** `renders/pov-{demo-stem}_{player}/`. Render resume is filesystem-based — existing `batch-*.mp4` ≥1MB are skipped (`--resume-from-round` deprecated). `--batches N` splits rounds into N CSDM calls (default 2); `--until N` stops after step N.
- **Structured errors:** failures print one JSON line — `[PIPELINE_ERROR] {"error":true,"step":N,"step_name":"...","code":"..."}`. Grep `[PIPELINE_ERROR]`; common codes: `EXTRACT_MAP_NOT_FOUND`, `RATINGS_NO_TABLES`, `ANALYZE_NO_ROUNDS`, `RENDER_STEAM_NOT_RUNNING`, `CONCAT_FAILED`, `THUMBNAIL_MISSING`, `HF_DOWNLOAD_FAILED`. Upload errors (`UPLOAD_NO_VIDEO_ID`) come from the upload scripts.
- **Overlay-only (default for ALL POVs):** every pipeline produces only the overlay variant `youtube/{run_id}_overlay/` (title suffix `| Input Overlay + Utility Cam`, badge + note in thumbnail/description) — the keyboard+util-cam version IS the product. `--raw-only` forces just the raw variant; each variant has its own `upload_meta.json`; re-running re-does only missing work.
- **Chaining:** `scripts/pov/pipeline_chain.py` starts the next POV when the watched one reaches `step >= 6` (`run_id` = `{match_id}_{demo_stem}_{player}_{map}`). Only one render at a time.

## Backlog Creation

`python scripts/pov/create_backlog.py <hltv_url>` — downloads the match demo(s) and generates rating-ranked backlog cards for every player/map combo: `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` (player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority, `hf_root`). Validates each `.dem` exists on disk before writing — raises `FileNotFoundError` instead of placeholders. Details: `docs/agents/pipeline.md`.

**FACEIT flow is split in two:** full match POVs — `scripts/faceit/create_faceit_match_backlog.py <demo_path>` analyzes the demo (`csdm json`) and creates cards **only for Recognised Pros** (`.data/player_accounts.json` by steam_id), each dropped into `backlog/faceit/{priority}/` by its in-match rating (`hltvRating2`; ≥1.5 high, ≥1.0 mid, else low — same thresholds as HLTV). No ELO; FACEIT matches are single-map so there's no per-match folder (match id stays in the filename + `faceit_match_id`). Each card carries `rating`, `kd`, `team`, `faceit_match_id`, `faceit_id`, `faceit_nickname`. Individual POV — `scripts/faceit/create_faceit_backlog.py <demo_path> --player <nick> --map <map>` (single card, same `backlog/faceit/{priority}/` layout) then the standard `pipeline.py`. The individual flow fetches current FACEIT ELO per demo player at creation time (`elo` + `opp_avg_elo` on the card; `--no-elo` skips) plus the player's in-match K/D (`kills`/`deaths`, computed from the demo's `player_death` events — knife round + suicides excluded, matching csdm). Its title shows **only** `"{player} ({kills}-{deaths}) | {map} | FACEIT CS2 POV` — no ELO rating, team names, tournament, or stage (pipeline reads ELO straight from the card for the description, so no API calls during render). Details: `docs/agents/pipeline.md`.

## CLI Entry Point

`python main.py <command>` — no other entry points.

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

Renders via **CSDM** (`C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd`) + **HLAE 2.190.1+** (`mirv_streams` encodes directly to video, no image sequences). Full csdm command + details: `docs/agents/rendering.md`.

- **Critical:** `--output` must be **absolute**. Relative paths resolve from the CS2 install dir → `AFXERROR: Failed writing image for screen recording` → "Raw files not found". `render_pov.py` and the pipeline always pass `Path.resolve()` dirs.
- Always use `python scripts/pov/render_pov.py <demo> <steam_id> [--batches 2]` — wraps csdm with round detection, p1/p2 handling, batch naming, and the crosshair swap (renders at 2560×1440).
- **VP9 trick:** render 2560×1440 even for 1080p-targeted uploads — YouTube gives 1440p+ VP9 (higher bitrate), sharper at 1080p too. All scripts default to 2560×1440. **Encode split:** `render_pov.py` (NVENC CQ 10) + `concat_rounds.py` (NVENC CQ 8) are mezzanine with a 200M cap — must not degrade before the final encode; `overlay_encode.py` is the **final export uploaded verbatim** (NVENC CQ 15 / 60M — max practical 1440p quality for overlay text/UI edges, clean for YouTube's re-encode). Overlay-only is the default, so the overlay's encode is the delivered bitstream.
- **Split demos (p1/p2):** auto-detected and rendered sequentially; `.rar` may contain multiple `.dem` (all extracted).
- **Concat:** `python scripts/pov/concat_rounds.py <renders_folder>` → `combined.mp4` (incremental batch-by-batch ffmpeg stream copy + upscale to 1440p via CUDA Lanczos; each batch deleted after append).

## Known Gotchas (critical subset — full list: `docs/agents/gotchas.md`)

- **NEVER clean up avatars** — `demos/avatars/` is a persistent cache reused across all matches. Never delete avatar files during cleanup.
- **Autoexec crosshair swap** — CS2 reads crosshair from `game/csgo/cfg/autoexec.cfg`; `assets/cs2_pov.cfg` execs it after keybind restore. `render_pov.py` swaps `autoexec_render.cfg` (pro crosshair) / `autoexec_personal.cfg` (yours) before/after rendering. If both missing, rename either to `autoexec.cfg`.
- **PBDEMS2 demos** (PGL tournaments) — analyze with `csdm analyze <demo> --source challengermode`.
- **Wrong HLTV match URL ID** — ratings scraper returns "Unknown Match"/empty tables when the match URL's numeric ID is wrong (HLTV is an SPA — the ID must match the JS routing). Check the correct ID via the match page sidebar "Related matches".
- **Windows PowerShell** — no `&&`, `||`, `tail`; use `; if ($?) {}` and `Select-String -Last 3`. `@'...'@` heredocs broken with f-strings — write Python scripts to files.
- **RAR extraction** — `rarfile` doesn't work on Windows. Use `patoolib` (via `patool` pip package) which wraps WinRAR.
- **All async** — every scraper/command is `asyncio.run()`. New commands must follow `async def` + `register_subparser` + `handle` in `commands/`.
- **YouTube scheduling** — **Long-form** (`upload_pending.py` / pipeline) default `--publish-at auto`: next future **10:00 or 16:30 Australia/Sydney** slot via the **YouTube API** (channel uploads playlist). **Shorts** (`upload_youtube_shorts.py`) reuse **CS2UtilArchive's schedule** (`scripts/publish_schedule.py` `SLOT_TIMES = ["17:30"]` + `find_next_upload_slot`) against the same occupied-slot pool, so both projects' shorts never double-book. Local ledger (`youtube/.publish_schedule.json`) deprecated. Explicit `--publish-at "YYYY-MM-DD HH:MM"` still schedules exactly.
- **YouTube verification** — custom thumbnails require a phone-verified account (https://www.youtube.com/verify).
- **HLTV Cloudflare block** — `net::ERR_CONNECTION_RESET` on HLTV while regular Chrome works = Cloudflare fingerprinting. Fix: Playwright with system Chrome + `ignore_default_args=["--enable-automation"]` (applied in `scrapers/hltv_acquire.py` and `scrapers/hltv.py`). Custom DNS does NOT help.
- **HLTV demo acquisition** — CloakBrowser, persistent profile `.sessions/hltv-cloak/`. Undersized archives (<1MB) are not cache hits. Fallback: `--demo` with local `.rar`/`.dem`.

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
- `docs/` — design/context notes (`CONTEXT.md`, batching, dual-upload) and `docs/adr/`; agent reference docs in `docs/agents/`
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
