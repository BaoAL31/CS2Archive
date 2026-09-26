# AGENTS.md — CS2Archive

> **Production path:** `scripts/hltv/match_listener.py` (or `scripts/pov/create_backlog.py`) → `scripts/pov/pipeline.py` → `scripts/upload/upload_pending.py`. `main.py` is acquisition only (HLTV/FACEIT download, player accounts, ratings). Individual step scripts exist for debugging or resuming a failed step — see below.

## Reference Docs (read on demand)

Operational deep-dives live in `docs/agents/`; this file is the quick reference:

| Doc | Contents |
|---|---|
| `docs/agents/pipeline.md` | Pipeline flags, resume, structured errors, overlay-only vs `--raw-only`, backlog creation, output layout |
| `docs/agents/steps.md` | Individual step scripts (debugging / manual use) |
| `docs/agents/rendering.md` | CSDM/HLAE/ffmpeg rendering details |
| `docs/agents/gotchas.md` | Full gotcha list |
| `docs/agents/shorts-titles.md` | YouTube Short title conventions + approved examples (creative, ELO/level-10 opponent labels) |
| `docs/agents/hltv-listener.md` | HLTV match listener, highlight/POV star weights, queue order |

Long-running **bugs** (open, not a how-to) live in `docs/bugs/`:

| Doc | Contents |
|---|---|
| `docs/bugs/hlae-steam-online-hook.md` | Steam-online HLAE inject/record flake (vanilla demo viewer, no ffmpeg) — not solved |

## Scripts layout

Scripts are grouped by product/concern (not a flat dump):

| Folder | Contents |
|---|---|
| `scripts/pov/` | POV Archive pipeline (`pipeline.py`, render, concat, backlog, …) |
| `scripts/overlay/` | Util-cam overlay (+ optional keyboard, lineup freeze frames) |
| `scripts/hltv/` | HLTV match listener, highlight-channel team demand, card scoring |
| `scripts/faceit/` | FACEIT POV helpers (titles, thumbnails, names, backlog, **daily FACEIT notable** `daily_notable.py`) |
| `scripts/highlights/` | Highlight Reel / Kill Timeline (Kinocut path — separate from POV) |
| `scripts/upload/` | YouTube + Bilibili publish |
| `scripts/hf/` | HuggingFace demo sync |
| `scripts/misc/` | One-offs |

Import bootstrap: `scripts/_pathsetup.py` (`ensure()` adds all buckets to `sys.path`).

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

## Hook (POV Cold Open)

No-spoiler highlight prepended to every POV in step 5, **before** the FACEIT intro card: the POV player's best moments rendered with `cl_draw_only_deathnotices 1` (killfeed only — scoreboard, round timer, money and team scores hidden; the Shorts HUD), crossfaded into one fast-paced cold open (~30s). Multiple moments allowed. Both HLTV and FACEIT.

```powershell
python scripts/pov/build_hook_timeline.py <demo.dem> --player <steam64>   # -> renders/hook-<stem>_<player>/hook_timeline.json (nothing written if nothing qualifies)
python scripts/pov/render_hook.py renders/hook-<stem>_<player>/hook_timeline.json
python scripts/pov/assemble_hook.py renders/hook-<stem>_<player>/hook_render.json --pov-video youtube/<run>_overlay/video.mp4
# -> renders/hook-<stem>_<player>/hook.mp4
```

- **Tiers** (`--tiers`, best first): `clutch_1v5` → `clutch_1v4` → `clutch_1v3` → `5k` → `punch_up` → `4k` → `insta_kill` → `clutch_attempt`. `clutch_attempt` = the POV player **lost** the 1vX; it leaks no result, and it is what makes hooks common (9/10 demos get ≥1 moment vs 3/10 without — measured on the multikill ladder, 10 demos: `clutch_attempt` 27, `4k` 3, `clutch_1v3` 1, `5k` 1, `clutch_1v4`/`1v5`/`punch_up` 0).
- **`insta_kill` is a different detector**: LOS time-to-kill from `scripts/overlay/victim_rewind.py` (`detect_from_demo(demo, player=<steam64>)`) — victim visible to the attacker for **≤ 0.5s** before dying (raycast against the map collision mesh from CS2UtilArchive), victim at **≥ 90 HP**, gun kill, **not a trade** (2s). Single-kill moments, which the multikill extractor cannot see at all — e.g. the donk/mirage demo has zero 4K/clutch but two `insta_kill` USP headshots (0.19s and 0.50s). Runs only when `insta_kill` is in `--tiers` (it is expensive: tick snapshots + raycasts).
- **`insta_kill` silent-failure guard:** `closest_hit` returns `None` both for "no obstruction" and "map mesh unavailable", and victim_rewind reads `None` as *LOS open* — a missing mesh therefore reads as "LOS always open", pushing every TTK past the cap and yielding **zero** insta kills with no error. The builder checks the mesh loads and warns loudly instead of silently returning nothing.
- The Shorts `perfect_shots` type (2–4 gun kills whose *shot count* ≈ kill count — ammo efficiency, **not** LOS/TTK) is **not** a hook tier. It is folded back into `4k` when it has ≥4 kills, because the detector *excludes* such a 1-bullet-per-kill 4K from the `4k` short — without that fold, those 4Ks would silently vanish from hooks.
- Detection is the Shorts extractor. Multikills only exist at **≥4 kills** and 4Ks need ≥2 victims on attacker-tier-or-better weapons (no farming), so real maps yield few moments — a demo with no qualifying moment is a **normal skip**, not an error, and the video ships without a hook.
- `hook_plan.py` plans **kill-anchored windows before rendering** (2s setup, 1.2s pre-kill, 1.5s post-kill, 2.5s payoff capped at 4s, adjacent windows merged) so each CSDM sequence is already a tight clip — no re-trim at assembly, no silent-segment risk.
- Renders under `autoexec_render.cfg` (this run's pro crosshair/viewmodel **+ HLAE spec-lock**), matching the POV footage. The spec-lock is what keeps the camera on the POV player after death — `clutch_attempt` moments usually end with them dead.
- Assembly is **one ffmpeg pass**: xfade + acrossfade chain, scaled to the POV video's exact W×H/fps, NVENC CQ15 / 60M (the overlay final-export profile) → the prepend into `video.mp4` stays a plain `-c copy` stream copy.
- `--no-hook` disables it; `--hook-tiers`, `--hook-max-moments` (3), `--hook-max-seconds` (30), `--hook-fade` (0.25), `--hook-min-round` (2) tune it. Delete `hook_timeline.json` to re-tune the tier threshold.
- **`--hook-min-round` (default 2):** round 1 sits ~30s into the finished video, so replaying it as a cold open is wasted (and round 0 is the knife round). A POV whose only hook-worthy moment is round 1 therefore ships with **no hook** — correct, not a failure.
- **Cached-timeline staleness:** the builder only re-runs when `hook_timeline.json` is missing, so the timeline records its `params` (tiers / min_round / max_moments / max_seconds) and the pipeline revalidates them — a changed filter **rebuilds** instead of silently reusing stale selections (`timeline_matches()`; pre-`params` caches rebuild once).

## Match Intro (16:9 Highlight Intro)

Separate product from both the Highlight Reel and `intro_prepend.py` (the static card + buy-phase lead-in). Picks the SINGLE most impressive moment of a demo and renders a 16:9 2560x1440 highlight: crossfade edit, capped at 60s. No title card.

```powershell
python scripts/faceit/build_intro_timeline.py demos/faceit/<demo>.dem --player <steam64>
# -> renders/hl-{demo_stem}/intro/intro_timeline.json
python scripts/faceit/render_intro.py renders/hl-{stem}/intro/intro_timeline.json --out youtube/<run>_overlay/intro.mp4
# -> the 16:9 highlight, placed in the POV's youtube folder (segments/ CSDM output alongside)
```

- Reuses the shorts extractor (`build_short_timeline` from `scripts/shorts/`) for detection.
- Qualifying: **1v3/1v4/1v5 clutches** (won rounds) and **5k multikills** (ACE). 4k multikills, 2v4/2v5 clutches and 1v3 punch-up triples are excluded.
- Ranking (pick ONE): clutches first — bigger disadvantage (1v5 > 1v4 > 1v3), then more kills — then 5k multikills. `--player <steam64|nick>` restricts to one player's moments (use for a specific POV).
- The builder keeps the FULL short window (all kills intact); the 60s cap + dead-time trimming happen at render time.
- Edit (MoviePy): **crossfades** (dissolve, no dip-to-black) wherever ≥15s pass with no kill — the dead middle is dropped, keeping a short buffer after the previous kill and before the next. Cap 60s (footage keeps its climactic ending).
- Final encode 2560x1440@60 `h264_nvenc` CQ 15 (same profile as the overlay final-export / `intro_prepend.py`).
- Defaults to Recognised Pros only (`.data/player_accounts.json`); `--include-all-players` lifts the gate.
- `render_intro.py --segment <mp4>` skips CSDM (debug / iterative editing without a demo render).
- CSDM render step needs Steam + CS2 (same infra as `render_shorts.py`); the segment is the resume unit (existing output ≥1MB skips assembly).

## Pipeline (Primary Entry Point)

`python scripts/pov/pipeline.py --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--until N]`

Runs steps 1-6 (analyze → render → concat → overlay → outro → thumbnail) from the backlog entry, writes `upload_meta.json` per variant. **Does NOT upload** — run `scripts/upload/upload_pending.py` afterward. Resumable via `.pipeline/{run_id}.json` (`--step N` resumes). Full details: `docs/agents/pipeline.md`.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | analyze | `csdm analyze <demo>` |
| 2 | render | `python scripts/pov/render_pov.py <demo> <steam_id>` |
| 3 | concat | `python scripts/pov/concat_rounds.py <renders_folder>` |
| **4** | **overlay** | `python scripts/overlay/overlay_pov.py --video <video.mp4> --demo <demo> --steam-id <id> [--round N]` |
| 5 | outro | `python scripts/pov/generate_outro.py <video.mp4>` (plus hook + FACEIT intro card, see below) |
| 6 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --video <mp4> --demo <dem> --steam-id <id>` (frame from finished overlay/raw video via sidecar; no CS2) |
| 7 | cleanup | `python scripts/pov/cleanup_renders.py <renders_folder>` |

Key rules:

- **Resume rule:** ALWAYS check `.pipeline/{run_id}.json` before deleting saved progress (combined.mp4, rendered clips, …) — it records the last completed step. Resume with the same backlog + `--step N`.
- **Uploading is separate.** Pipeline stops at step 6 with `upload_status="pending"`; `upload_pending.py` uploads every pending `youtube/*/upload_meta.json` (resume-safe: skips `completed`).
- **Intermediates kept until upload (purge moved post-upload):** pipeline keeps `renders/pov-*` after step 6 (repair path for re-overlay/re-scale); `upload_pending.py` purges the render dir once **every** variant (raw + overlay) is uploaded (`--keep-renders` opts out of the purge). Per-round clips are still freed after step 4 (combined.mp4 kept). Opt back into purge-at-end with `pipeline.py --cleanup` (step 7).
- **Overlay (step 4) runs by default:** utility throw flight PiP clips (~1–2 min/throw via CSDM/HLAE; 20+ throws ≈ 30–60 min), one PiP per util type per video (first smoke/flash/HE/fire shows; later same-type throws never repeat — spots irrelevant), restricted to non-straightforward throws (straightforward tosses skipped). PiPs are **keycap-free**: they read the clean `flight_<throw>.mp4` standalone, and the render disables CS2UtilArchive's input-overlay burn (`burn_input_overlay=False` in `render_util_cams.py`) — required because for single-camera he/flash jobs the deliverable path IS that standalone file. High-arc tosses that stay in the thrower's own air volume for ≥250u (`LOFT_EXIT_MAX`, `is_intuitive_lob`) count as straightforward open lobs, not lineups. Lineup freeze frames in the main POV are OFF by default — opt in with `--freeze` (pipeline + `overlay_pov.py`); the freeze pre-pass expands the sidecar for batch/voice, while the compositor maps against the pristine timeline and pushes each PiP past the holds so it never drifts. Keyboard input overlay is OFF by default — opt in with `--keyboard` (pipeline + `overlay_pov.py`). Skip the whole step with `--until 3` or `--raw-only`. `--overlay-only` is a deprecated no-op.
- **Input overlay source (only with `--keyboard`):** keyboard states come from CS2UtilArchive's overlay kernel (usercmd decode), not a local `scripts/overlay/usercmd_extract.py`. The sibling checkout is `settings.cs2util_root` in `scripts/config.py`.
- **`--skip-failed-rounds` — [DANGER] NEVER set by default.** Only for corrupted/incompatible demos (e.g. `100-thieves-vs-spirit-m3-dust2.dem` — fails round 1 with "Game error" for every player). Silently drops failed rounds → incomplete POV. Enabled per-invocation or via backlog `pipeline_cmd` / `skip_failed_rounds: true`.
- **Demo download:** omit `--demo` to download from `hltv_url` (CloakBrowser), or from HuggingFace if backlog has `hf_root` (single `.dem` from `cs2povarchive/cs2-demos`); pass `.rar`/`.dem` to skip; `--force` re-downloads. HF pull failure → `HF_DOWNLOAD_FAILED`.
- **Steam ID** comes from the backlog entry, resolved by `create_backlog.py` from `.data/player_accounts.json` (via `main.py player add/list`) — no `--steam-id` CLI flag. Extract from demo: `scripts/pov/extract_steamids.py <demo_path>`.
- **Render folder per POV:** `renders/pov-{demo-stem}_{player}/`. Render resume is filesystem-based — existing `batch-*.mp4` ≥1MB are skipped (`--resume-from-round` deprecated). `--batches N` splits rounds into N CSDM calls (default 1 — renders all rounds in a single CS2/HLAE launch); `--until N` stops after step N.
- **Structured errors:** failures print one JSON line — `[PIPELINE_ERROR] {"error":true,"step":N,"step_name":"...","code":"..."}`. Grep `[PIPELINE_ERROR]`; common codes: `EXTRACT_MAP_NOT_FOUND`, `RATINGS_NO_TABLES`, `ANALYZE_NO_ROUNDS`, `RENDER_STEAM_NOT_RUNNING`, `CONCAT_FAILED`, `THUMBNAIL_MISSING`, `HF_DOWNLOAD_FAILED`, hook codes (`HOOK_TIMELINE_FAILED`, `HOOK_RENDER_FAILED`, `HOOK_ASSEMBLE_FAILED`, `HOOK_PREPEND_FAILED`). Version-gate codes (`RENDER_HLAE_CS2_MISMATCH`, `RENDER_CS2_UNPINNED`, `RENDER_HLAE_OUTDATED`, `RENDER_CSDM_OUTDATED`, …) come from `scripts/pov/render_version_check.py`; see `docs/bugs/hlae-steam-online-hook.md`. Upload errors (`UPLOAD_NO_VIDEO_ID`) come from the upload scripts.
- **Overlay-only (default for ALL POVs):** every pipeline produces one overlay video at `youtube/{run_id}_overlay/` (util-cam badge + note in thumbnail/description; `W/ INPUT OVERLAY` pill and input tags only with `--keyboard`) and one `upload_meta.json`. `--raw-only` writes `youtube/{run_id}/` with no overlay. Re-running re-does only missing work.
- **Chaining:** the listener renders one POV at a time and starts the next after upload spawn. `scripts/pov/pipeline_chain.py` is a leftover manual helper, not used in production.

## Backlog Creation

`python scripts/pov/create_backlog.py <hltv_url>` — downloads the match demo(s) and generates rating-ranked backlog cards for every player/map combo: `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` (player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority, `hf_root`). Validates each `.dem` exists on disk before writing — raises `FileNotFoundError` instead of placeholders. Same pass extracts Recognised-Pro Shorts (`--no-shorts` skips), then drops low-demand POVs that would burn the two daily Shorts slots (YouTube demand index, or a NAVI / Spirit / Vitality hook in the match — not Falcons); only demos with at least one remaining short get a folder under `renders/shorts/shorts-{demo_stem}/`. Pending uploads that fail the same gate are marked `skipped`. Details: `docs/agents/pipeline.md`.

**FACEIT flow is split in two:** full match POVs — `scripts/faceit/create_faceit_match_backlog.py <demo_path>` analyzes the demo (`csdm json`) and creates cards **only for Recognised Pros** (`.data/player_accounts.json` by steam_id), each dropped into `backlog/faceit/{priority}/` by its in-match rating (`hltvRating2`; ≥1.5 high, ≥1.0 mid, else low — same thresholds as HLTV). Match cards also store POV ELO + opposing-team average (`elo` / `opp_avg_elo`) unless `--no-elo`. FACEIT matches are single-map so there's no per-match folder (match id stays in the filename + `faceit_match_id`). Each card carries `rating`, `kills`, `deaths`, `kd`, `team`, `faceit_match_id`, `faceit_id`, `faceit_nickname`. Individual POV — `scripts/faceit/create_faceit_backlog.py <demo_path> --player <nick> --map <map>` (single card, same `backlog/faceit/{priority}/` layout) then the standard `pipeline.py`. The individual flow fetches current FACEIT ELO per demo player at creation time (`elo` + `opp_avg_elo` on the card; `--no-elo` skips) plus the player's in-match K/D (`kills`/`deaths`, computed from the demo's `player_death` events — knife round + suicides excluded, matching csdm). Its title includes ELO when the card has it (e.g. `NiKo 5512 ELO vs ~3470 ELOs | Mirage | FACEIT CS2 POV`). Pipeline reads ELO from the card (no API calls during render). Details: `docs/agents/pipeline.md`.

**Daily FACEIT notable:** `scripts/faceit/scrape_notable.py` discovers multi-pro + single-pro standout matches and **scores every Recognised-Pro POV**. `daily_notable.discover_good_povs()` is what the listener polls: last **24h**, only watchable POVs (**demand-index star >= 1.25**; extra Recognised Pros and K/D scale bonuses, not the gate), no padding. `--download` fetches each picked demo + builds a single-POV backlog card. Not a Windows scheduled task.

**HLTV match listener:** `scripts/hltv/match_listener.py` polls completed event results and queues **one weighted card per match** from `backlog/<match>/{high,medium}/` (rating >= 1.0). Weight = highlight-channel team demand + POV-channel player demand + org rank + HLTV rating + this fixture's recent highlight views. Cap is **2 uploads per local day**. If the event has nothing live and nothing starting in the next 12 hours, it keeps polling FACEIT for watchable POVs (demand-index star >= 1.25; losses count; extra Recognised Pros are a score chip) and queues them as they appear, up to remaining daily slots — it does not pad with weak games. After each successful pipeline it opens a **new console** running `scripts/upload/upload_pending.py --dir <overlay_dir> --limit 1` for that POV and starts the next render in the same listener process. Refresh stars with `scripts/hltv/refresh_stars.py` (daily 12:00 Windows task `CS2ArchiveStarRefresh`: scrapes competitor POV channels + @cs2povarchive, then highlight channels). Inspect a match with `python scripts/hltv/score_cards.py backlog/<match_slug>`. Details: `docs/agents/hltv-listener.md`.

## CLI Entry Point

`python main.py <command>` — **acquisition only** (download demos, player accounts, ratings, status). Rendering and upload live under `scripts/`.

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
- All config in `scripts/config.py` (pydantic-settings, loads from `.env`)
- **Python env:** uses same `cs2archive` conda env as sibling project. In non-interactive shells (OpenCode, CI), `conda activate` often fails — use direct-path bypass:
  ```powershell
  $env:PYTHONPATH="."; & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py <args>
  ```

## Shared Demo Store (CS2UtilArchive)

`CS2UtilArchive/demos/extracted` is an **NTFS junction to `demos/hltv`** — one physical store with the same `<match_id>-<slug>/` folder layout. Demos landed by either project are instantly visible to the other: backlog/pipeline existence checks just work, and `overlay_utilcams._ensure_cs2util_data`'s copy bridge is now a no-op. CS2UtilArchive's `download_demos.py` extracts per-slug into the shared store and **skips downloads when demos already exist there** — so no HLTV re-fetch happens for matches either project already has. Recreate the junction after a fresh clone with:

```cmd
mklink /J "D:\Projects\CS2UtilArchive\demos\extracted" "D:\Projects\CS2Archive\demos\hltv"
```

**Caveat:** CS2UtilArchive's HF-corpus cleanup deletes `.dem` files from dirs it marked `.hf_downloaded` — including inside this shared store. Unmarked HLTV demos are never auto-deleted. Do not delete `.dem` files or `demos/` dirs without asking (both repos' rules).

## Demo Video Rendering

Renders via **CSDM** (`C:\Users\jembo\AppData\Local\Programs\cs-demo-manager\csdm.cmd`) + **HLAE** pinned to the running CS2 build (`mirv_streams` encodes directly to video, no image sequences). Full csdm command + details: `docs/agents/rendering.md`.

- **Critical:** `--output` must be **absolute**. Relative paths resolve from the CS2 install dir → `AFXERROR: Failed writing image for screen recording` → "Raw files not found". `render_pov.py` and the pipeline always pass `Path.resolve()` dirs.
- Always use `python scripts/pov/render_pov.py <demo> <steam_id> [--batches 1]` — wraps csdm with round detection, p1/p2 handling, batch naming, and the crosshair swap (captures at the POV's prosettings resolution — 4:3 1280×960 for stretched players — then `concat_rounds.py` upscales to 2560×1440).
- **VP9 trick:** render 2560×1440 even for 1080p-targeted uploads — YouTube gives 1440p+ VP9 (higher bitrate), sharper at 1080p too. Capture follows the POV's prosettings resolution (backlog `capture_width`/`capture_height`; CLI `--width`/`--height` overrides) and `concat_rounds.py` stretches it to the 2560×1440 upload target. **Encode split:** `render_pov.py` (NVENC CQ 10) + `concat_rounds.py` (NVENC CQ 8) are mezzanine with a 200M cap — must not degrade before the final encode; `overlay_encode.py` is the **final export uploaded verbatim** (NVENC CQ 15 / 60M — max practical 1440p quality for overlay text/UI edges, clean for YouTube's re-encode). Overlay-only is the default, so the overlay's encode is the delivered bitstream.
- **Split demos (p1/p2):** auto-detected and rendered sequentially; `.rar` may contain multiple `.dem` (all extracted).
- **Concat:** `python scripts/pov/concat_rounds.py <renders_folder>` → `combined.mp4` (incremental batch-by-batch ffmpeg stream copy + upscale to 1440p via CUDA Lanczos; each batch deleted after append).

## Known Gotchas (critical subset — full list: `docs/agents/gotchas.md`)

- **NEVER clean up avatars** — `demos/avatars/` is a persistent cache reused across all matches. Never delete avatar files during cleanup.
- **NEVER restart Steam** — `scripts/misc/steam_mode.py --offline/--online` runs `steam.exe -shutdown` first. The agent shell's `Get-Process steam` / `tasklist` is a **false negative** (Steam is running, the listing is empty). Do not start/stop/offline-toggle Steam unless the user explicitly says it is down. Pipeline `RENDER_STEAM_NOT_RUNNING` from the render process is the real check.
- **Autoexec crosshair swap** — CS2 reads crosshair from `game/csgo/cfg/autoexec.cfg`; `assets/cs2_pov.cfg` execs it after keybind restore. `render_pov.py` swaps `autoexec_render.cfg` (pro crosshair) / `autoexec_personal.cfg` (yours) before/after rendering. If both missing, rename either to `autoexec.cfg`.
- **PBDEMS2 demos** (PGL tournaments) — analyze with `csdm analyze <demo> --source challengermode`.
- **Wrong HLTV match URL ID** — ratings scraper returns "Unknown Match"/empty tables when the match URL's numeric ID is wrong (HLTV is an SPA — the ID must match the JS routing). Check the correct ID via the match page sidebar "Related matches".
- **Windows PowerShell** — no `&&`, `||`, `tail`; use `; if ($?) {}` and `Select-String -Last 3`. `@'...'@` heredocs broken with f-strings — write Python scripts to files.
- **RAR extraction** — `rarfile` doesn't work on Windows. Use `patoolib` (via `patool` pip package) which wraps WinRAR.
- **All async** — every scraper/command is `asyncio.run()`. New commands must follow `async def` + `register_subparser` + `handle` in `commands/`.
- **YouTube scheduling** — **Long-form** (`upload_pending.py` / pipeline) default `--publish-at auto`: next future **10:00 or 16:30 Australia/Sydney** slot via the **YouTube API** (channel uploads playlist), for up to 2 long-form uploads per day. **Shorts** (`upload_youtube_shorts.py`) reuse **CS2UtilArchive's schedule** (`scripts/publish_schedule.py` `SLOT_TIMES = ["18:00"]` + `find_next_upload_slot`, **one Short/day**) against the same occupied-slot pool, so both projects' shorts never double-book. Local ledger (`youtube/.publish_schedule.json`) deprecated. Explicit `--publish-at "YYYY-MM-DD HH:MM"` still schedules exactly.
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
- `docs/` — design/context notes (batching, dual-upload) and `docs/adr/`; agent reference docs in `docs/agents/`
- `assets/` — static resources (map images, fonts, CS2 config files).
- `grafipy-out/` — knowledge graph (from `/graphify`). Not project code.

## Skills Available

Installed from mattpocock/skills (see `skills-lock.json`):
- `/grill-me` — stress-test a plan via relentless questioning
- `/grill-with-docs` — same but also updates `docs/adr/` + ADRs
- `/handoff` — compact session into handoff doc for next agent
- `/caveman` — ultra-compressed mode
- `/wikify` — generate a Karpathy-style wiki from a thesis, project, paper, or report. Extracts concepts, methods, papers, datasets, and architecture into interlinked Markdown wiki articles.
