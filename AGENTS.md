# AGENTS.md — CS2Archive

> **Production path:** `cs2archive.listener.daemon` (or `cs2archive.pov.create_backlog`) → `cs2archive.pov.pipeline` → `cs2archive.upload.upload_pending`. `main.py` is acquisition only (HLTV/FACEIT download, player accounts, ratings). Individual step modules exist for debugging or resuming a failed step — see below.
>
> **Layout (CR-01):** the code is an installed package, `cs2archive/`, not a folder of loose scripts. Run a module with `python -m cs2archive.<bucket>.<module>`; 10 entry points also have console scripts (`cs2archive-pipeline`, `cs2archive-refresh-stars`, …) installed into the conda env's `Scripts/`. `commands/`, `scrapers/` and `thumbnail/` remain top-level packages. Module depth is identical to the old `scripts/<bucket>/` layout, so `Path(__file__).parents[1]`/`parents[2]` still resolve to the repo root. The old `scripts/<bucket>/<module>.py` paths exist as **DEPRECATED shims** for documented entry points only (they print a notice on stderr); delete them at the next cleanup.

## Reference Docs (read on demand)

Operational deep-dives live in `docs/agents/`; this file is the quick reference:

| Doc | Contents |
|---|---|
| `docs/agents/pipeline.md` | Pipeline flags, resume, structured errors, overlay-only vs `--raw-only`, backlog creation, output layout |
| `docs/agents/steps.md` | Individual step scripts (debugging / manual use) |
| `docs/agents/rendering.md` | CSDM/HLAE/ffmpeg rendering details |
| `docs/agents/gotchas.md` | Full gotcha list |
| `docs/agents/shorts-titles.md` | YouTube Short title conventions + approved examples (creative, ELO/level-10 opponent labels) |
| `docs/agents/shorts-picker.md` | Allstar predicted-view loss, global candidate ranking, two-successes/day render quota and recovery |
| `docs/agents/match-listener.md` | Unified match listener (HLTV results + FACEIT notables), highlight/POV star weights, queue order |

Long-running **bugs** (open, not a how-to) live in `docs/bugs/`:

| Doc | Contents |
|---|---|
| `docs/bugs/hlae-steam-online-hook.md` | Steam-online HLAE inject/record flake (vanilla demo viewer, no ffmpeg) — not solved |
| `docs/bugs/audio-sync-atempo-drift.md` | Recurring late POV audio: automatic fitter / near-unity `atempo` added drift; safety fix, real-capture regression, recovery and re-enable gates |

## Scripts layout

## Package layout

The code lives in the `cs2archive/` package, grouped by product/concern:

| Package | Contents |
|---|---|
| `cs2archive/pov/` | POV Archive pipeline (`pipeline.py`, render, concat, backlog, …) |
| `cs2archive/overlay/` | Util-cam overlay (+ optional keyboard, lineup freeze frames) |
| `cs2archive/render/` | Shared HLAE/CSDM render infrastructure (`hook_aware.py`, used by 4 products) |
| `cs2archive/listener/` | Unified match listener daemon (`daemon.py`: HLTV results + FACEIT notables) + its run/stop/install-task wrappers |
| `cs2archive/hltv/` | HLTV-only helpers: star refresh/eval, card scoring, team demand |
| `cs2archive/faceit/` | FACEIT POV helpers (titles, thumbnails, names, backlog, **daily FACEIT notable** `daily_notable.py`) |
| `cs2archive/highlights/` | Highlight Reel / Kill Timeline (Kinocut path — separate from POV) |
| `cs2archive/upload/` | YouTube + Bilibili publish |
| `cs2archive/hf/` | HuggingFace demo sync |
| `cs2archive/misc/` | One-offs |
| `commands/`, `scrapers/`, `thumbnail/` | Top-level packages (folded into `cs2archive/` in a later pass) |

Packaging: `pyproject.toml`; install with `pip install -e . --no-deps` (deliberately no-deps so pip does not re-resolve the conda env). **There is no import bootstrap any more** — `scripts/_pathsetup.py` and every `sys.path` insertion for our own modules are gone. The only remaining `sys.path` writes are for the sibling CS2UtilArchive checkout (`cs2archive/overlay/_common.py:prefer_cs2util_scripts` and the matching sites in `render_util_cams`/`victim_rewind`/`build_hook_timeline`).

## Lint and CI (CR-13)

```powershell
uvx ruff@0.16.9 check .                                  # the lint gate; no env changes needed
python .github/scripts/check_waivers.py                  # every suppression must be tokened+dated
python .github/scripts/check_known_failures.py           # fail on test failures not in the baseline
python .github/scripts/check_known_failures.py --update  # re-baseline after an intentional change
```

- The lint gate selects only crash-level rules: **`F` + `E9`**. Style rules are deliberately out.
- **Waivers are the ratchet.** `F401` (216), `F541` (51) and `F841` (20) are waived globally in
  `pyproject.toml`, and five per-file waivers suppress findings inside files that the concurrent
  agent holds uncommitted. Every waiver **must** carry a `CR-<n>` token and an
  `expires YYYY-MM-DD` date; `check_waivers.py` fails CI on an untokened or expired one, so a
  suppression cannot outlive its ticket.
- **`F841` is not purely cosmetic** — three sites are in tests where a computed-but-unused value
  may be a forgotten assertion (`tests/test_pipeline.py:201`, `tests/test_overlay_sidecar_skip_failed.py:232`
  and `:235`). Triage each under CR-15; do not bulk-delete.
- Known test failures live in `tests/known_failures.txt` (14 entries, CR-13 baseline). New failures
  fail the local gate. On a hosted CI runner the test job is advisory because it cannot reproduce
  `.data/`/`.cache/` or the dependency set; set `CS2ARCHIVE_ENFORCE_TESTS=1` on a self-hosted runner
  to make it blocking.
- **Silent-except ratchet (CR-09):** `python .github/scripts/report_silent_excepts.py` measures
  S110 (`try/except/pass`) + BLE001 (blind except) and fails only when the count **exceeds** the
  ceiling in `.github/silent_except_ceiling.txt` (445 measured 2026-09-27). The debt can therefore
  only shrink; lower the ceiling with `--update` as sites are fixed. Kill/verify/render-decision
  sites are the ones worth fixing first — each is a potential false "clean" or false "failed".
- Quarantine status: **`tests/test_overlay_extraction.py` only** — a CLI script whose `test_*`
  helpers were being mis-collected as tests (collection suppressed in `tests/conftest.py`; pytest
  9.1.1 rejects the ini form of `collect_ignore_glob`). The two intermittent `test_swift_demoui.py`
  tests turned out to be **real-machine-state coupling, not flakes**, and are fixed: each now pins
  `_live_render_processes` explicitly to state whether it is asserting the mount-refusal path or the
  normal mount path. CR-16.

## Highlights (Kill Timeline) — FACEIT only

Separate product from POV Archive. v1 flow: action timeline → LLM edit timeline → CSDM segment renders → single reel (per-segment avatar cut-ins + crossfades).

```powershell
python -m cs2archive.highlights.build_action_timeline demos/faceit/<demo>.dem
# -> renders/hl-{demo_stem}/action_timeline.json
python -m cs2archive.highlights.build_edit_timeline demos/faceit/<demo>.dem
# -> renders/hl-{demo_stem}/edit_timeline.json  (LLM-batched, post-shaped by _fix_edit_timeline)
python -m cs2archive.highlights.render_edit_timeline renders/hl-<stem>/edit_timeline.json
# -> renders/hl-{stem}/segments/seg-NNN-pov-<sid>-tick-<a>-to-<b>.mp4  (batch-config CSDM, 1920x1080@64, resume-safe)
python -m cs2archive.highlights.assemble_reel renders/hl-<stem>/edit_timeline.json
# -> renders/hl-{stem}/reel.mp4
```

- Hard-refuses demos outside `demos/faceit/` (action timeline)
- Every kill where **at least one** of attacker or victim is a Recognised Pro (`.data/player_accounts.json`). Includes unknown→pro picks.
- Recognised Pros = `player_accounts.json` only (no `faceit_pros.json`)
- `render_edit_timeline.py` renders via CSDM `--config-file` batches; segment files are the resume unit (existing ≥1MB skipped).
- `assemble_reel.py` normalizes segments to 60fps, bakes each segment's POV player avatar (transparent cutout + white outline, bottom-centre — same treatment as `render_shorts.py`), then concatenates with `xfade`/`acrossfade` (default 0.4s) into `reel.mp4`. Intermediates in `reel_tmp/` (resumable).

## Hook (POV Cold Open)

No-spoiler highlight prepended to every POV in step 5, **before** the FACEIT intro card: the POV player's best moments rendered with the full player HUD but the compact alive-count team bar (`cl_teamcounter_playercount_instead_of_avatars`, N vs N) and the score digits blurred at assembly (the Shorts HUD), jump-cut into one fast-paced cold open (~30s). Multiple moments allowed. Both HLTV and FACEIT.

```powershell
python -m cs2archive.pov.build_hook_timeline <demo.dem> --player <steam64> --pov-dir renders/pov-<stem>_<nick>   # -> {pov}/hook/hook_timeline.json (nothing written if nothing qualifies)
python -m cs2archive.pov.render_hook renders/pov-<stem>_<nick>/hook/hook_timeline.json
python -m cs2archive.pov.assemble_hook renders/pov-<stem>_<nick>/hook/hook_render.json --pov-video youtube/<run>_overlay/video.mp4
# -> renders/pov-<stem>_<nick>/hook/hook.mp4
```

- **Tiers** (`--tiers`, best first): `clutch_1v5` → `clutch_1v4` → `clutch_1v3` → `5k` → `punch_up` → `4k` → `punch_up_single` → `insta_kill` → `duel` → `clutch_attempt` → `opener` → `trade`. `clutch_attempt` = the POV player **lost** the 1vX; it leaks no result, and it is what makes hooks common (9/10 demos get ≥1 moment vs 3/10 without — measured on the multikill ladder, 10 demos: `clutch_attempt` 27, `4k` 3, `clutch_1v3` 1, `5k` 1, `clutch_1v4`/`1v5`/`punch_up` 0).
- **`punch_up_single` (pistol vs rifle, head):** attacker pistol (tier 1) beats a rifle (victim tier ≥ 4), headshot — that alone qualifies, lone kills included (e.g. a Deagle headshot onto an AK holder with no second kill nearby). First contact is **scoring input, not a gate**: `insta_kill` rows (LOS-TTK ≤ 0.35s) stack full insta points on top, `peek` rows (measured contact ≤ 0.5s but over the insta cap) stack less, untracked re-kills score punch-up only. Victim weapons come from the POV-local action timeline (`renders/pov-<stem>_<nick>/action_timeline.json`, self-healed by the hook builder into the pov folder); rewind rows only enrich the TTK. Same-round kills chain-merge with best tier winning.
- **`duel` / `opener` / `trade` (action-timeline moments, bottom tiers):** POV-slice moments involving the player under a non-victim role (bomb/util moments never qualify). Kill sets are POV-kills-only — teammates' kills in the window (e.g. teamkills inside a duel engagement) are stripped, and kill-less moments are dropped. Singles only ship clustered via the 5s kill-gap chaining + 15s fill bar.
- **Per-demo action timeline:** `ensure_action_timeline()` in `cs2archive/highlights/build_action_timeline.py` builds + caches `renders/hl-{stem}/action_timeline.json` for the multi-pros highlights pipeline; the POV flow keeps its own copy at `renders/pov-<stem>_<nick>/action_timeline.json` (built on demand by the hook builder, ~10s cold) — `pov_action_slice()` reads the POV player's kills/moments. The thumbnail kill-frame picker reads the same cache: density stays primary, full-buy rounds / punch-up / headshots break ties (`kill_bg_bonus` in `thumbnail/utils.py`).
- **`insta_kill` first-contact guard:** an earlier open run inside the 2s LOS lookback disqualifies the kill (`has_prior_exposure` — needs ≥2 consecutive OPEN samples, so single-sample mesh cracks don't count). A repeek prefire after the POV already saw the enemy is anticipation, not reaction. The insta cap is **0.35s** (22 ticks); measured first contact inside 0.5s but over the cap surfaces as a `peek` row (no insta moment — punch-up proof only). Flick solos are exempt (a snap is a snap). Rule changes bump `INSTA_RULE_VERSION` (now 8) so stale `hook_timeline.json` caches rebuild; tier-list changes rebuild automatically via the stored `params.tiers`.
- **`insta_kill` is a different detector**: LOS time-to-kill from `cs2archive/overlay/victim_rewind.py` (`detect_from_demo(demo, player=<steam64>)`) — victim visible to the attacker for **≤ 0.35s** before dying (raycast against the map collision mesh from CS2UtilArchive), gun kill, **not a trade** (2s — checked against everyone's kills, since the POV filter would hide the teammate's death that makes it one). Victim HP is deliberately NOT a gate (reaction time is the product, not lethality). Lone kills ship as **solo** moments; runs of 2+ kills (≤3s apart, same round, distinct victims, ≥1 victim HP≥90) chain into one pair moment, failed-pair runs split back into solos. Single-kill moments, which the multikill extractor cannot see at all — e.g. the donk/mirage demo has zero 4K/clutch but fast USP headshots. Runs only when `insta_kill` is in `--tiers` (it is expensive: tick snapshots + raycasts). Rule changes bump `INSTA_RULE_VERSION` so stale `hook_timeline.json` caches rebuild.
- **`insta_kill` silent-failure guard:** `closest_hit` returns `None` both for "no obstruction" and "map mesh unavailable", and victim_rewind reads `None` as *LOS open* — a missing mesh therefore reads as "LOS always open", pushing every TTK past the cap and yielding **zero** insta kills with no error. The builder checks the mesh loads and warns loudly instead of silently returning nothing.
- The Shorts `perfect_shots` type (2–4 gun kills whose *shot count* ≈ kill count — ammo efficiency, **not** LOS/TTK) is **not** a hook tier. It is folded back into `4k` when it has ≥4 kills, because the detector *excludes* such a 1-bullet-per-kill 4K from the `4k` short — without that fold, those 4Ks would silently vanish from hooks.
- Detection is the Shorts extractor. Multikills only exist at **≥4 kills** and 4Ks need ≥2 victims on attacker-tier-or-better weapons (no farming), so real maps yield few moments — a demo with no qualifying moment is a **normal skip**, not an error, and the video ships without a hook.
- **Target fill (`--hook-min-seconds`, default 10):** the builder accumulates chains best-first (quality order) until the *planned* footage reaches 10s — the minimum segments to fill it (at most `--hook-max-moments` chains, `--hook-max-seconds` uncut). Short of the bar: no hook ships (normal skip, same as no moment). The `--hook-max-seconds` budget is enforced on planned footage, not detection spans (a two-kill moment can span ~60s of detection while shipping 4s — budgeting spans evicted good chains on phantom footage).
- `hook_plan.py` plans **kill-anchored windows before rendering** (1s pre-kill, 1s post-kill/payoff, adjacent windows merged) so each CSDM sequence is already a tight clip — no re-trim at assembly, no silent-segment risk. Uncapped lone singles therefore plan exactly 2.00s each, so the 10s bar takes ~5 segments.
- Renders under its **own** `autoexec_render.cfg`, written by `render_hook.py` from the **same inputs as the POV render** (canonical nick from `player_accounts.json` by steam_id → prosettings crosshair + viewmodel, rename map, HLAE spec-lock) — the hook only changes the HUD (full HUD + compact N-vs-N team bar, score blurred at assembly — blur boxes in `cs2archive/hud_score_blur.py`), never the cfg. The timeline's raw demo nick is canonicalized at build time; a changed plan/cfg re-renders stale segments instead of reusing them.
- **Chat/console text is hidden** in every full-HUD render via `cs2archive/chat_hide.py` (`cl_showtextmsg 0` + `hidehud 128`, written into the **launch autoexec**, not just the per-sequence cfg): the relayed server lines (`Console: This server's password has been changed to: …`) are `TextMsg` HUD prints issued while the demo plays, so CSDM's fast seek can leave them inside their display window at the start of a segment. `tv_nochat` only covers SourceTV spectator chat and `cl_chatfilters`/`tv_relaytextchat`/`hud_saytext_time` do not exist in CS2.
- Assembly is **one ffmpeg pass**: `concat` join (jump cuts, no dissolves), scaled to the POV video's exact W×H/fps, tail faded to black over `--hook-fade`, NVENC CQ15 / 60M (the overlay final-export profile). Moments assemble **least-impressive-first** by explicit `quality`, where a chain's quality is its **best single segment** (`chain_segments()`: each planned window scored alone; a window enclosing ALL the moment's kills keeps the full moment score — tier × 1000 + kills × 100 + headshot bonus (flat 50 per headshot kill, counted once per kill tick no matter how many fused candidates reference it — chained kills add up, so 2 headshots outscore 1) + **peek-kill bonus** (tapered 100→0 over victim-hold 15°→45°: small hold = the victim was already holding the angle, the POV peeked into a prepared crosshair — back/side shots get nothing; per kill, deduped per tick) + **flick-speed bonus** (`clamp((deg/s − 100) × 0.75, 0, 150)`, measured dense per-tick over the 24 ticks before the kill via wrap-free view vectors; max over the moment, never summed) + TTK speed − **trade deter** (100 per traded kill tick — the ceiling of the insta reaction scale, so a revenge kill forfeits exactly what an insta earns; survives chain merges) — while a fragment window covering only some kills scores just what it encloses (no tier base: +100/kill, +50/headshot, +peek, −trade), so scattered multi-kill pieces rank as the weak singles they play as instead of pooling tier + count at moment level — tier always dominates *within* a continuous clip, but never across clips), so the hook builds to its climax instead of peaking early. Back-to-back moments (kills <5s apart) **chain** into one continuous clip instead of cutting (union bounds + kills, best tier wins, max 15s span); wider gaps stay separate clips. Every tail (single and multikill) ends 0.4s before the POV's next kill, so a payoff can't swallow it and read as a bigger multikill.
- Prepends join video by stream copy but **decode + filter-join + re-encode audio to AAC 48k stereo**: segments come from mixed encoders (CSDM MP3 44.1k vs NVENC AAC 48k) and a `-c copy` audio join leaves packets past the cut undecodable (silence after the hook). Same reason the overlay remux normalizes source audio to AAC 48k instead of copying it.
- `--no-hook` disables it; `--hook-tiers`, `--hook-max-moments` (8), `--hook-max-seconds` (60), `--hook-min-seconds` (10), `--hook-fade` (0.5), `--hook-min-round` (2) tune it. Delete `hook_timeline.json` to re-tune the tier threshold.
- **`--hook-min-round` (default 2):** round 1 sits ~30s into the finished video, so replaying it as a cold open is wasted (and round 0 is the knife round). A POV whose only hook-worthy moment is round 1 therefore ships with **no hook** — correct, not a failure.
- **Cached-timeline staleness:** the builder only re-runs when `hook_timeline.json` is missing, so the timeline records its `params` (tiers / min_round / max_moments / max_seconds) and the pipeline revalidates them — a changed filter **rebuilds** instead of silently reusing stale selections (`timeline_matches()`; pre-`params` caches rebuild once).

## Match Intro (16:9 Highlight Intro)

Separate product from both the Highlight Reel and `intro_prepend.py` (the static card + buy-phase lead-in). Picks the SINGLE most impressive moment of a demo and renders a 16:9 2560x1440 highlight: crossfade edit, capped at 60s. No title card.

```powershell
python -m cs2archive.faceit.build_intro_timeline demos/faceit/<demo>.dem --player <steam64>
# -> renders/hl-{demo_stem}/intro/intro_timeline.json
python -m cs2archive.faceit.render_intro renders/hl-{stem}/intro/intro_timeline.json --out youtube/<run>_overlay/intro.mp4
# -> the 16:9 highlight, placed in the POV's youtube folder (segments/ CSDM output alongside)
```

- Reuses the shorts extractor (`build_short_timeline` from `cs2archive/shorts/`) for detection.
- Qualifying: **1v3/1v4/1v5 clutches** (won rounds) and **5k multikills** (ACE). 4k multikills, 2v4/2v5 clutches and 1v3 punch-up triples are excluded.
- Ranking (pick ONE): clutches first — bigger disadvantage (1v5 > 1v4 > 1v3), then more kills — then 5k multikills. `--player <steam64|nick>` restricts to one player's moments (use for a specific POV).
- The builder keeps the FULL short window (all kills intact); the 60s cap + dead-time trimming happen at render time.
- Edit (MoviePy): **crossfades** (dissolve, no dip-to-black) wherever ≥15s pass with no kill — the dead middle is dropped, keeping a short buffer after the previous kill and before the next. Cap 60s (footage keeps its climactic ending).
- Final encode 2560x1440@60 `h264_nvenc` CQ 15 (same profile as the overlay final-export / `intro_prepend.py`).
- Defaults to Recognised Pros only (`.data/player_accounts.json`); `--include-all-players` lifts the gate.
- `render_intro.py --segment <mp4>` skips CSDM (debug / iterative editing without a demo render).
- CSDM render step needs Steam + CS2 (same infra as `render_shorts.py`); the segment is the resume unit (existing output ≥1MB skips assembly).

## Pipeline (Primary Entry Point)

`python -m cs2archive.pov.pipeline --backlog backlog/<match_slug>/<priority>/<slug>.json [--step N] [--until N]`

Runs steps 1-6 (analyze → render → concat → overlay → outro → thumbnail) from the backlog entry, writes `upload_meta.json` per variant, then **hands the video to YouTube itself**: it spawns `cs2archive/upload/upload_pending.py --dir <overlay> --limit 1` in a new console (resumable, fire-and-forget) exactly like the listener does. `--no-upload` opts out (the listener passes it — the listener owns the spawn so it can charge its daily-slot ledger). Resumable via `.pipeline/{run_id}.json` (`--step N` resumes). Full details: `docs/agents/pipeline.md`.

| Step | Name | Script for manual use / debugging |
|---|---|---|
| 1 | analyze | `csdm analyze <demo>` |
| 2 | render | `python -m cs2archive.pov.render_pov <demo> <steam_id>` |
| 3 | concat | `python -m cs2archive.pov.concat_rounds <renders_folder>` |
| **4** | **overlay** | `python -m cs2archive.overlay.overlay_pov --video <video.mp4> --demo <demo> --steam-id <id> [--round N]` |
| 5 | outro | `python -m cs2archive.pov.generate_outro <video.mp4>` (plus hook + FACEIT intro card, see below) |
| 6 | thumbnail | `python -m thumbnail <url> --player <nick> --map <map> --video <mp4> --demo <dem> --steam-id <id>` (frame from finished overlay/raw video via sidecar; no CS2) |
| 7 | cleanup | `python -m cs2archive.pov.cleanup_renders <renders_folder>` |

Key rules:

- **Resume rule:** ALWAYS check `.pipeline/{run_id}.json` before deleting saved progress (combined.mp4, rendered clips, …) — it records the last completed step. Resume with the same backlog + `--step N`.
- **Uploading is automatic.** The pipeline spawns the upload after step 6 (`--no-upload` opts out); the listener spawns it itself and therefore passes `--no-upload` to the pipelines it runs. `upload_pending.py` is the resumable worker either way — it skips `completed` metas, retries crashed uploads, and purges the render dir once every variant is uploaded. Re-running it by hand is only for repair.
- **Intermediates kept until upload (purge moved post-upload):** pipeline keeps `renders/pov-*` after step 6 (repair path for re-overlay/re-scale); `upload_pending.py` purges the render dir once **every** variant (raw + overlay) is uploaded (`--keep-renders` opts out of the purge) — `shorts/` inside is spared (separate upload flow). Per-round clips are still freed after step 4 (combined.mp4 kept). Opt back into purge-at-end with `pipeline.py --cleanup` (step 7).
- **Overlay (step 4) runs by default:** utility throw flight PiP clips (~1–2 min/throw via CSDM/HLAE; 20+ throws ≈ 30–60 min), one PiP per distinct lineup (**first** occurrence of each map:type:side:landing cluster shows — same util repeated never PiPs twice, distinct spots of the same type each get a PiP; no blind/damage ranking), restricted to non-straightforward throws (straightforward tosses skipped; short-arc blocked-LOS tosses count as straightforward). `--straightforward-filter` (pipeline + `overlay_pov.py`) re-enables it (default off since 2026-09-28 while the gate is considered fragile; `--no-straightforward-filter` is a deprecated alias for the default). PiP render + expected-clip validator honor it, freeze pre-pass still classifies. Flight clips hold 1.5s past detonate for HE/flash (64 ticks smoke/fire). PiPs are **keycap-free**: they read the clean `flight_<throw>.mp4` standalone, and the render disables CS2UtilArchive's input-overlay burn (`burn_input_overlay=False` in `render_util_cams.py`) — required because for single-camera he/flash jobs the deliverable path IS that standalone file. High-arc tosses that stay in the thrower's own air volume for ≥250u (`LOFT_EXIT_MAX`, `is_intuitive_lob`) count as straightforward open lobs, not lineups. Lineup freeze frames in the main POV are OFF by default — opt in with `--freeze` (pipeline + `overlay_pov.py`); the freeze pre-pass expands the sidecar for batch/voice, while the compositor maps against the pristine timeline and pushes each PiP past the holds so it never drifts. Keyboard input overlay is ON by default — opt out with `--no-keyboard` (pipeline + `overlay_pov.py`). Skip the whole step with `--until 3` or `--raw-only`. `--overlay-only` is a deprecated no-op.
- **Input overlay source (only with `--keyboard`):** keyboard states come from CS2UtilArchive's overlay kernel (usercmd decode), not a local `cs2archive/overlay/usercmd_extract.py`. The sibling checkout is `settings.cs2util_root` in `cs2archive/config.py`. Button look is `--overlay-style` (`classic` legacy green, `broadcast` solid amber, `ghost` outline-only; default `auto` picks by map contrast — amber on Nuke, ghost-cyan on warm sand maps) and press motion is `--overlay-anim` (`instant` cut default, `decay`/`soft` release tails); presets live in CS2UtilArchive `scripts/render/overlay_assets.py`.
- **`--skip-failed-rounds` — [DANGER] NEVER set by default.** Only for corrupted/incompatible demos (e.g. `100-thieves-vs-spirit-m3-dust2.dem` — fails round 1 with "Game error" for every player). Silently drops failed rounds → incomplete POV. Enabled per-invocation or via backlog `pipeline_cmd` / `skip_failed_rounds: true`.
- **Demo download:** omit `--demo` to download from `hltv_url` (CloakBrowser), or from HuggingFace if backlog has `hf_root` (single `.dem` from `cs2povarchive/cs2-demos`); pass `.rar`/`.dem` to skip; `--force` re-downloads. HF pull failure → `HF_DOWNLOAD_FAILED`.
- **Steam ID** comes from the backlog entry, resolved by `create_backlog.py` from `.data/player_accounts.json` (via `main.py player add/list`) — no `--steam-id` CLI flag. Extract from demo: `cs2archive/pov/extract_steamids.py <demo_path>`.
- **Render folder per POV:** `renders/pov-{demo-stem}_{player}/` — the whole world for one POV: `hook/` (cold open), `intro/` (intro card + footage), `shorts/` (that POV's shorts), `stat-strips/` (repeek pane cache for the match, copied from a sibling POV when present instead of re-captured), `action_timeline.json` (hook + thumbnail cache), render clips + sidecars. Render resume is filesystem-based — existing `batch-*.mp4` ≥1MB are skipped (`--resume-from-round` deprecated). `--batches N` splits rounds into N CSDM calls (default 1 — renders all rounds in a single CS2/HLAE launch); `--until N` stops after step N. `renders/hl-*` belongs to the multi-pros highlights pipeline only — the POV flow never creates it. Path helpers live in `cs2archive/paths.py` (purges spare `shorts/`).
- **Structured errors:** failures print one JSON line — `[PIPELINE_ERROR] {"error":true,"step":N,"step_name":"...","code":"..."}`. Grep `[PIPELINE_ERROR]`; common codes: `EXTRACT_MAP_NOT_FOUND`, `RATINGS_NO_TABLES`, `ANALYZE_NO_ROUNDS`, `RENDER_STEAM_NOT_RUNNING`, `CONCAT_FAILED`, `THUMBNAIL_MISSING`, `HF_DOWNLOAD_FAILED`, hook codes (`HOOK_TIMELINE_FAILED`, `HOOK_RENDER_FAILED`, `HOOK_ASSEMBLE_FAILED`, `HOOK_PREPEND_FAILED`). Version-gate codes (`RENDER_HLAE_CS2_MISMATCH`, `RENDER_CS2_UNPINNED`, `RENDER_HLAE_OUTDATED`, `RENDER_CSDM_OUTDATED`, …) come from `cs2archive/pov/render_version_check.py`; see `docs/bugs/hlae-steam-online-hook.md`. Upload errors (`UPLOAD_NO_VIDEO_ID`) come from the upload scripts.
- **Overlay-only (default for ALL POVs):** every pipeline produces one overlay video at `youtube/{run_id}_overlay/` (util-cam badge + note in thumbnail/description; `W/ INPUT OVERLAY` pill and input tags only with `--keyboard`) and one `upload_meta.json`. `--raw-only` writes `youtube/{run_id}/` with no overlay. Re-running re-does only missing work.
- **Chaining:** the listener renders one POV at a time and starts the next after upload spawn. `cs2archive/pov/pipeline_chain.py` is a leftover manual helper, not used in production.

## Backlog Creation

`python -m cs2archive.pov.create_backlog <hltv_url>` — downloads the match demo(s) and generates rating-ranked backlog cards for every player/map combo: `backlog/{match_slug}/{priority}/{player}-{map}-{match_slug}.json` (player, map, steam_id, demo_path, hltv_url, tournament, avatar_path, ratings_path, rating, kd, team, priority, `hf_root`). Validates each `.dem` exists on disk before writing — raises `FileNotFoundError` instead of placeholders. Same pass extracts Recognised-Pro Shorts candidates (`--no-shorts` skips) under their POV folders. HLTV extraction no longer applies long-form demand/intercept vetoes or claims daily slots: `render_pending_shorts` ranks the entire pending pool by predicted typical Allstar views and permits at most two successful renders per Sydney day. Upload remains separate on the shared one-Short/day schedule. Details: `docs/agents/pipeline.md` and `docs/agents/shorts-picker.md`.

**FACEIT flow is split in two:** full match POVs — `cs2archive/faceit/create_faceit_match_backlog.py <demo_path>` analyzes the demo (`csdm json`) and creates cards **only for Recognised Pros** (`.data/player_accounts.json` by steam_id), each dropped into `backlog/faceit/{priority}/` by its in-match rating (`hltvRating2`; ≥1.5 high, ≥1.0 mid, else low — same thresholds as HLTV). Match cards also store POV ELO + opposing-team average (`elo` / `opp_avg_elo`) unless `--no-elo`. FACEIT matches are single-map so there's no per-match folder (match id stays in the filename + `faceit_match_id`). Each card carries `rating`, `kills`, `deaths`, `kd`, `team`, `faceit_match_id`, `faceit_id`, `faceit_nickname`. Individual POV — `cs2archive/faceit/create_faceit_backlog.py <demo_path> --player <nick> --map <map>` (single card, same `backlog/faceit/{priority}/` layout) then the standard `pipeline.py`. The individual flow fetches current FACEIT ELO per demo player at creation time (`elo` + `opp_avg_elo` on the card; `--no-elo` skips) plus the player's in-match K/D (`kills`/`deaths`, computed from the demo's `player_death` events — knife round + suicides excluded, matching csdm). Its title includes ELO when the card has it (e.g. `NiKo 5512 ELO vs ~3470 ELOs | Mirage | FACEIT CS2 POV`). Pipeline reads ELO from the card (no API calls during render). Details: `docs/agents/pipeline.md`.

**Daily FACEIT notable:** `cs2archive/faceit/scrape_notable.py` discovers multi-pro + single-pro standout matches and **scores every Recognised-Pro POV**. `daily_notable.discover_good_povs()` is what the listener polls: last **24h**, only watchable POVs (**demand-index star >= 1.40 with sample evidence behind the entry** — `demand_star_supported`: recent videos or a deep track record, so stale long-window-only thin samples don't auto-pass; extra Recognised Pros and K/D scale bonuses, not the gate), no padding. `--download` fetches each picked demo + builds a single-POV backlog card. Not a Windows scheduled task.

**Match listener (HLTV + FACEIT):** `cs2archive/listener/daemon.py` (was `cs2archive/hltv/match_listener.py`; old path is a deprecated shim) polls completed HLTV event results **and** FACEIT notables in one process, and queues **one card per match** (`select_best_card`: highest HLTV rating across the series' maps — a two-map series must not spend both daily slots) from `backlog/<match>/{high,medium}/` (rating >= 1.0). HLTV is now cherry-picked like FACEIT: `_star_gate_cards` drops any POV whose player fails the FACEIT demand rule (`_star_eligible` → `scoring.demand_eligibility` at 1.40, star **with sample evidence** or a breakout; fail-closed on a stale payload, drops logged), and actionable matches are actioned **best-fixture-first** by `_match_demand_points` (both teams' demand + recent `match_highlight` views) instead of /results page order. Escapes: `--no-star-gate` (rating only) and `--results-order`. The top-20 team list is the notable-team gate and refreshes automatically once its cached copy is >24h old (`--no-refresh-teams` pins it). Cap is **2 uploads per local day**. If the event has nothing live and nothing starting in the next 12 hours, it polls FACEIT for whatever the quality gate passes each scrape (`n=room`, up to the remaining daily slots; no FACEIT-specific per-day cap — the star filter decides the count, roughly one quality POV per day, more when several standouts appear). Repeats are suppressed by persisted `used` performance ids. After each successful pipeline it opens a **new console** running `cs2archive/upload/upload_pending.py --dir <overlay_dir> --limit 1` for that POV and starts the next render in the same listener process. Refresh stars with `cs2archive/hltv/refresh_stars.py` (daily 12:00 Windows task `CS2ArchiveStarRefresh`: scrapes competitor POV channels + @cs2povarchive, then highlight channels). Grade the star itself against that same scrape with `python -m cs2archive.hltv.eval_stars` (time-split, held-out competitor POV performance; player-level spearman + permutation p, gate lift, precision@K, baselines). Inspect a match with `python -m cs2archive.hltv.score_cards backlog/<match_slug>`. Details: `docs/agents/match-listener.md`.

## CLI Entry Point

`python main.py <command>` — **acquisition only** (download demos, player accounts, ratings, status). Rendering and upload live in the `cs2archive/` package.

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
- All config in `cs2archive/config.py` (pydantic-settings, loads from `.env`)
- **Python env:** uses same `cs2archive` conda env as sibling project. In non-interactive shells (OpenCode, CI), `conda activate` often fails — use direct-path bypass:
  ```powershell
  $env:PYTHONPATH="."; & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" cs2archive/pov/pipeline.py <args>
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
- Always use `python -m cs2archive.pov.render_pov <demo> <steam_id> [--batches 1]` — wraps csdm with round detection, p1/p2 handling, batch naming, and the crosshair swap (captures at the aspect-correct machine maximum — `cs2archive/capture_res.py`, the pro's own resolution is ignored — then `concat_rounds.py` upscales to 2560×1440).
- **VP9 trick:** render 2560×1440 even for 1080p-targeted uploads — YouTube gives 1440p+ VP9 (higher bitrate), sharper at 1080p too. **Capture resolution is the aspect-correct machine maximum, not the pro's** (`cs2archive/capture_res.py`, CR-17): the POV's aspect ratio is all that survives from `player_accounts.json` — 4:3 renders 1440×1080, 16:9 1920×1080, 16:10 1728×1080, 5:4 1350×1080 on the 1080p rig — and `concat_rounds.py` **stretches** it (never letterbox/pillarbox) to the 2560×1440 upload target. Height must stay within the desktop: requesting above it makes HLAE clamp the height but keep the width, silently changing the aspect (`1920x1440` request → `1920x1080` 16:9 clip, measured 2026-10). CLI `--width`/`--height` still overrides (and the pipeline treats them as the *export* size). Shorts (`render_shorts._resolve_player_resolution`) and the 16:9 Match Intro (`faceit/render_intro.py`) resolve their capture size through the same helper, so every product renders at the same resolution for a given POV; the intro's own canvas mapping (`_scale_to_canvas`) is unchanged. **Encode split:** `render_pov.py` (NVENC CQ 10) + `concat_rounds.py` (NVENC CQ 8) are mezzanine with a 200M cap — must not degrade before the final encode; `overlay_encode.py` is the **final export uploaded verbatim** (NVENC CQ 15 / 60M — max practical 1440p quality for overlay text/UI edges, clean for YouTube's re-encode). Overlay-only is the default, so the overlay's encode is the delivered bitstream.
- **Split demos (p1/p2):** auto-detected and rendered sequentially; `.rar` may contain multiple `.dem` (all extracted).
- **Concat:** `python -m cs2archive.pov.concat_rounds <renders_folder>` → `combined.mp4` (incremental batch-by-batch ffmpeg stream copy + upscale to 1440p via CUDA Lanczos; each batch deleted after append; obvious-defuse round ends dissolve into the next round pre-scale, 1.2s xfade, later offsets shift).
- **Auto-team avatars:** the old automatic `--hide-avatars` (FACEIT card with an empty `tournament` → `cl_hide_avatar_images 1` + playercount instead of avatar panels) is **removed** — the pipeline no longer appends the flag, so the scoreboard always renders the per-player avatar panels. `--hide-avatars` still exists on `render_pov.py` / `render_hook.py` / `intro_prepend.py` as a manual escape hatch for lobbies whose Steam avatars resolve to missing-texture checkers.
- **Overlay GOP discipline:** `overlay_encode.py` forces `-g 60`; batch resume trusts a versioned stamp (params/boundaries/source), never size alone; stream-copy batches additionally require a clean head (first-packet IDR, monotonic PTS) or fall back to null re-encode; the final concat fails loudly on PTS regression or any keyframe gap >6s (`_audit_gop` — cap sits above the legitimate 4.17s CSDM source GOP, far below real holes).

## Known Gotchas (critical subset — full list: `docs/agents/gotchas.md`)

- **NEVER clean up avatars** — `demos/avatars/` is a persistent cache reused across all matches. Never delete avatar files during cleanup.
- **NEVER restart Steam** — `cs2archive/misc/steam_mode.py --offline/--online` runs `steam.exe -shutdown` first. The agent shell's `Get-Process steam` / `tasklist` is a **false negative** (Steam is running, the listing is empty). Do not start/stop/offline-toggle Steam unless the user explicitly says it is down. Pipeline `RENDER_STEAM_NOT_RUNNING` from the render process is the real check.
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
- `cs2archive/downloader.py` — file management, archive extraction, download history JSON.
- `cs2archive/models.py` — Pydantic models shared across modules.
- `thumbnail/` — thumbnail generator package (Pillow-based compositing, 1280×720 output).
- `cs2archive/` — the pipeline package, grouped by product (`pov/`, `overlay/`, `render/`, `faceit/`, `highlights/`, `hltv/`, `upload/`, `hf/`, `shorts/`, `misc/`) plus the shared modules (`config.py`, `models.py`, `downloader.py`, `player_accounts.py`, `scoring.py`, `crosshair_*.py`, `csdm_*.py`, `_backlog_common.py`). Installed via `pyproject.toml`.
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
