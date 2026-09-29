# Thermo-Nuclear Code Quality Review — whole repo

- **Date:** 2026-09-27
- **Commit reviewed:** `eba2f87` — "Start hooks at round 2 and rebuild stale hook timelines"
- **Scope:** whole repository (`commands/`, `scrapers/`, `thumbnail/`, `tests/`, `scripts/**`, `main.py`)
- **Standard:** Cursor thermo-nuclear code quality review (ambitious structural simplification; no spaghetti growth; no files crossing 1000 lines without strong justification; no thin wrappers; logic in the canonical layer; explicit type boundaries; atomic/parallel orchestration where the cleaner structure is obvious)
- **Verdict:** **not approvable as-is.** Correctness is not in question; structure is. Three presumptive blockers are met (CR-01, CR-02, CR-03).

> **Status update, 2026-09-27 (same day):** **CR-01 is implemented, reviewed by the council, and
> closed** — see `council-log.md`. Its patch also closed part of CR-08 (a shadowed duplicate method
> and an `importlib` workaround) and part of CR-13 (`pyproject.toml` now exists), and surfaced
> **CR-14**, a real silent-failure bug in the `insta_kill` mesh guard, which is fixed. All other
> findings remain open. The line numbers below are a snapshot of `eba2f87`.

Nothing was changed in the repository during this review.

---

> ## ▶ RESUME HERE (status 2026-09-27, late)
>
> **The live, canonical status is the register table in `docs/reviews/README.md`; the full record of
> every decision and review round is in `docs/reviews/council-log.md`.** This report is the original
> snapshot of commit `eba2f87` and its per-finding evidence.
>
> | Finding | Status | Note |
> |---|---|---|
> | CR-01 package/`sys.path` | ✅ done | 168 files moved, 583 imports rewritten, shims, 545 cards migrated |
> | CR-02 `detect_shorts` 832-line function | ⬜ open | biggest remaining item; also where CR-03(a,b) land |
> | CR-03 duplicated domain logic | ◐ partly | **(d)** demand reader consolidated ✅ · (a) tick/winner helpers and (b) round-tail/constants still open |
> | CR-04 encoder/json/proc helpers | ◐ partly | (a) `encode.py` profiles ✅ + golden test · (b) jsonio and (c) proc codemods still open |
> | CR-05 untyped boundaries | ⬜ open | |
> | CR-06 pipeline double-implements steps | ⬜ open | |
> | CR-07 hardcoded settings paths | ✅ done | 2 new fields + 3 derived; values byte-verified |
> | CR-08 dead weight | ✅ done | 15 provably-dead files deleted, 13 kept with reasons |
> | CR-09 silent failures | ◐ partly | ratchet in CI (ceiling 460, may only shrink) + kill/verify sites fixed in `hook_aware`; ~460 sites remain |
> | CR-10 process-query conflation | ✅ done | 14 call sites, 23 tests |
> | CR-11 cross-product cycle | ✅ done | `cs2archive/weapons.py`; cycle broken both directions |
> | CR-12 encoder drift | ✅ done | reel → documented FINAL profile (rate cap added — documented in `rendering.md`) |
> | CR-13 no lint/CI | ✅ done | ruff `F`+`E9`, waiver audit, known-failures delta, GH Actions |
> | CR-14 insta_kill mesh guard | ✅ done | found during CR-01 review |
> | CR-15 two live NameErrors | ✅ done | `render_pov.py` fixed, its `F821` waiver deleted |
> | CR-16 suite determinism | ◐ mostly | swift_demoui tests fixed at the root; only the `test_*` helper rename left |
> | CR-17 shorts test cluster | ⬜ open | 5 baselined failures, one root cause |
> | CR-18 concurrent-render kill ownership | ⬜ open | needs per-render pid ownership (pipeline scope) |
>
> **Next action when resuming:** CR-03(c) — one round-tail implementation plus the duplicated
> `BOMB_LIFETIME_TICKS` / `DEFUSE_PRE_TAIL_TICKS` / `_CT_TIME_WIN_REASONS`, moving `round_windows.py` to
> the package root (council-approved; 1-line import change in `pov/pipeline.py`, which the other
> session owns). Then CR-04(b,c), then CR-02.
>
> **Process note for whoever resumes:** decisions go to the council (space-bunny + muse in Herdr) and
> so do *reviews of the patch*. Skipping the review half is how a real regression slipped through once
> (`render_edit_timeline` lost `-b:v 0`); it was caught only when the review round was finally run.
> Verify delegated claims before acting — two fabricated claims have been caught this session.


## Verdict

CS2Archive is ~40k lines of pipeline code that works, held together by three load-bearing
habits that now cost more than they save:

1. **It is not a package.** `scripts/` is both a PEP420 namespace package and a `sys.path`
   entry, giving every bucket module two import identities. 140 sites hack `sys.path` to
   make this work, two production workarounds exist purely to route around the resulting
   module shadowing, and the two files in one package import the same module two different
   ways.
2. **The same logic lives in two to four places.** Detection rules, round-tail trimming,
   winner derivation, tick→round resolution, demand tables, encode profiles and an SGD
   trainer each have multiple implementations. Some have already drifted (CR-12).
3. **Records are untyped.** 8 `@dataclass` in the repo, against 251 `: dict` parameters
   and 84 `dict`-returning functions. 94 distinct string keys travel through one pipeline
   state blob.

There is no lint gate, no CI, and no `pyproject.toml`, so nothing catches the class of
defect that CR-08 (a shadowed duplicate method) and CR-12 (undocumented encode profiles)
represent.

The good news: this is unusually amenable to structural repair. Six code-judo moves delete
well over 1500 lines and several whole categories of bug, and the first one (packaging) is
mechanical.

---

## Findings

Severity: **blocker** (blocks approval) · **major** · **minor**.
All findings are `[verified]` unless a lead is explicitly marked otherwise.

### ✅ CR-01 — `sys.path` bootstrap and no package: dual import identity for `scripts` — blocker `[verified]` **(DONE 2026-09-27)**

`scripts/_pathsetup.py:9,20-25` inserts the project root, `scripts/`, and each of nine bucket
directories into `sys.path`. Because `scripts/` is *also* a `sys.path` entry **and** has no
`__init__.py` (only `hltv/`, `overlay/`, `shorts/` of the nine buckets have one), every bucket
module is importable twice.

The clearest evidence — two files in the same package, same directory:

```python
scripts/highlights/build_action_timeline.py:41   from faceit_names import canonical_nick, known_pro_steam_ids
scripts/highlights/build_edit_timeline.py:34     from scripts.faceit.faceit_names import known_pro_steam_ids
```

And `pipeline.py` uses both identities in one file (`:48` bare `config`, `:156` and `:192`
`scripts.faceit.*`).

Supporting facts:

- **140** manual `sys.path.insert/append` sites outside `_pathsetup.py`, spread over ~80 files;
  plus `main.py:22-25`, `tests/conftest.py:11`, and further inserts inside individual tests.
- Duplicate importable basenames: `commands/utils.py` vs `thumbnail/utils.py`;
  `commands/{faceit,hltv,ratings,trending}.py` vs `scrapers/{faceit,hltv,ratings,trending}.py`.
  Resolution depends on `sys.path` order.
- The shadowing has already forced two production workarounds:
  - `scripts/pov/pipeline.py:127-135` hand-rolls an `importlib.util.spec_from_file_location`
    loader whose docstring reads *"Load CS2Archive's minimizer, not CS2UtilArchive's shadowed
    module."*
  - `scripts/overlay/_common.py:22-34` (`prefer_cs2util_scripts`) removes entries from
    `sys.path` and deletes `sys.modules["scripts"]` so that the sibling CS2UtilArchive
    checkout's `scripts` package resolves instead.
- `scripts/pov/create_backlog.py:368` generates code that imports `scripts.overlay._common`,
  so both identities are also baked into generated artifacts.
- `scripts/hook_aware.py` — imported by six files across four products
  (`pov/render_pov.py`, `pov/render_hook.py:53`, `shorts/render_shorts.py:385`,
  `faceit/intro_prepend.py:227`, `highlights/render_edit_timeline.py:462`,
  `overlay/swift_demoui.py:319`) — sits at `scripts/` root and resolves only as a bare
  top-level module. It is multi-product render infrastructure with no package to live in.

**Remedy.** One installable package (`cs2archive/`) with `__init__.py` throughout, subpackages
mirroring the current buckets, `pyproject.toml` with `[project.scripts]` entrypoints. Delete
`_pathsetup.py` and all 140 sites. Give the shared HLAE/CSDM infrastructure a real home
(a `render/` or `cs2archive/render/` layer) instead of `scripts/` root.

### ⬜ CR-02 — Ten files over 1000 lines; `detect_shorts()` is a single 832-line function — blocker `[verified]` **(OPEN)**

| File | Lines | Notes |
|---|---|---|
| `scripts/pov/pipeline.py` | 2443 | 57 functions, 273 `if`, max indent depth 11 |
| `scripts/shorts/build_short_timeline.py` | 1917 | `detect_shorts()` at `:832` is **832 lines**, 223 `if`, depth 12 |
| `scripts/highlights/build_edit_timeline.py` | 1740 | `_fix_edit_timeline()` `:1206` 361 lines; `_ensure_round_closers()` `:805` 295 lines |
| `scripts/overlay/overlay_pov.py` | 1657 | `run_overlay()` `:956` 658 lines; 4 functions take >6 args |
| `scripts/pov/render_pov.py` | 1453 | `main()` `:984` 466 lines; depth 14 |
| `scrapers/faceit.py` | 1413 | four unrelated concerns in one module (see below) |
| `scripts/hltv/match_listener.py` | 1312 | HTML parsing + state + locking + scoring + subprocess spawn |
| `tests/test_build_short_timeline.py` | 1267 | single test module |
| `scripts/highlights/build_action_timeline.py` | 1238 | `build_action_timeline()` `:709` 498 lines; `_derive_moments()` `:301` 406 lines |
| `scripts/shorts/render_shorts.py` | 1058 | `_composite_9x16()` `:523` 228 lines |

`detect_shorts()` is the worst artifact in the repository. It performs ~12 sequential
`# ==== X DETECTION ====` passes (4K `:1072`, wallbang `:1142`, clutch `:1277`, 1v3 `:1299`,
knife/zeus, defuse, perfect shots, flick, …) plus input normalization, and the shape

```python
for _rn, rkills in sorted(kills_by_round.items()):
    ...
    by_attacker = ...
```

appears **five times** (`grep -c` = 5), each pass re-deriving data the previous pass already had.

`scrapers/faceit.py` mixes four concerns: REST client (`FACEITClient:39`), Downloads-API client
(`FACEITDownloadsClient:296`), local filesystem archive watching (`_wait_for_match_archive:521`,
`_finished_match_archive:493`), and CDP/Playwright browser automation (`_ensure_cdp_chrome:738`,
`_auth_page:699`, `_login_button:715`).

**Remedy.** See Code-judo 2 (detector registry) and Code-judo 5 (split `faceit.py` into
`client` / `downloads` / `browser` / `fs_watch`).

### ◐ CR-03 — Domain logic duplicated across products — blocker `[verified]` **(partly: CR-03(d) done)**

- **Tick→round resolution has four implementations:** `shorts/build_short_timeline.py:803`
  (nested) and `:1698` (module), `highlights/build_action_timeline.py:106`,
  `pov/build_hook_timeline.py:245`.
- **Winner derivation is duplicated and self-admits it:** `_winner_by_round_from_demo`
  (`shorts/build_short_timeline.py:742`) vs `_authoritative_winners` +
  `_side_map_by_round` (`highlights/build_action_timeline.py:234`, `:199`). The action-timeline
  comment at `:246` states it is *"mirroring the shorts builder."*
- **Round-tail trimming for two products:** `pov/round_windows.py` (save tails, obvious-defuse
  tails) vs `highlights/build_edit_timeline.py:920 _trim_defuse_tail` and `:960
  _trim_round_end_tail` — the latter two being closures nested inside a 295-line function.
- **Copy-pasted constants:**
  - `BOMB_LIFETIME_TICKS = 2624` — `pov/round_windows.py:36` and `highlights/build_edit_timeline.py:465`
  - `DEFUSE_PRE_TAIL_TICKS = 192` — `pov/round_windows.py:38` and `highlights/build_edit_timeline.py:466`
  - `_CT_TIME_WIN_REASONS` (identical frozenset) — `shorts/build_short_timeline.py:54` and
    `highlights/build_action_timeline.py:140`
- **Two weapon vocabularies:** `_WEAPON_TIER: dict[str, int]` (`shorts/build_short_timeline.py:84`)
  vs `_CS2_WEAPON_IDS: dict[int, str]` (`highlights/build_action_timeline.py:45`).
- **Two demand tables / loaders:** `scoring.py:17 PLAYER_DEMAND_INDEX` and
  `scoring.py:44 load_player_demand_index`, duplicated in the FACEIT scraper layer
  (`faceit/scrape_notable.py:39,146`) and reached into by the gate
  (`shorts/demand_gate.py:97,100` does `from scrape_notable import PLAYER_DEMAND_INDEX`).
  `demand_gate._load_payload()` also re-implements the loader. A gate importing a *scraper*
  for a data table is a layer inversion.
- **Two SGD trainers:** `shorts/fit_clip_weights.py` and `shorts/fit_pov_weights.py` each define
  `new_weights`, `predict_log_views`, `sgd_epoch`, `evaluate`, plus rank-correlation helpers
  (`fit_clip_weights.py:86,91,101,151,197`; `fit_pov_weights.py:150,154,189,215,239`).
  `shorts/ml_common.py` exists but carries only three leaf helpers.
- **Reversed dependency:** `overlay/victim_rewind.py:20` imports `PRE_TICKS` from
  `shorts.flick`, and `:459` imports from `shorts.build_short_timeline`. Generic overlay
  compositing depends on the shorts product.

**Remedy.** One `demo_facts` module (round/winner/side-map/tick helpers), one `round_windows`
API used by both POV and highlights, one `weapon_rules` module, one `demand` module, one ML
trainer. Detailed in Code-judo 3, 4.

### ◐ CR-04 — Encoder, subprocess, JSON and retry helpers re-implemented per file — major `[verified]` **(partly: (a) encode.py profiles DONE; (b) jsonio + (c) proc open)**

- `h264_nvenc` argument lists are hand-typed in **19 files** (`render_pov.py` ×5,
  `render_shorts.py` ×9, `overlay_encode.py` ×2, `render_edit_timeline.py` ×2,
  `assemble_reel.py` ×2, `render_intro.py` ×2, `intro_prepend.py` ×2, `pipeline.py` ×2, and
  one each in `concat_rounds.py`, `generate_outro.py`, `render_hook.py`, `assemble_hook.py`,
  `save_transition.py`, `voice_shade.py`, `lineup_freeze.py`, `speaker_rows_preview.py`,
  `upload_bilibili.py`, `render_pending_shorts.py`, `test_encode_quality.py` ×11) — despite
  `docs/agents/rendering.md` defining exactly two profiles (mezzanine CQ10/8 200M, final
  CQ15 60M). This has already drifted: see CR-12.
- **ffprobe wrappers:** `overlay_pov.py:227`, `faceit/faceit_thumbnail.py:104`,
  `assemble_reel.py:67,77`, `render_edit_timeline.py:60,71` `[lead]`.
- **JSON I/O:** **70** `.read_text(encoding="utf-8")` sites (verified); ~44
  `write_text(json.dumps(...))` sites `[lead]`.
- **Sibling Python launch:** 12+ copies — `pipeline.py:719 _run_py`, `upload_pending.py:52`,
  `upload_pending_shorts.py:38`, `match_listener.py:789,806,880`, `pipeline_chain.py:24`,
  `create_backlog.py:404,601`, `daily_notable.py:62`, `overlay_utilcams.py:286`,
  `fit_partial_stars.py:386`, `render_pending_shorts.py:122`, `publish_due_social.py:54`,
  `misc/regenerate_meta.py:50` `[lead]`.
- **Retry-with-fallback:** `build_edit_timeline.py:360-447` (nvidia → openrouter),
  `match_listener.py:788-801`, `render_edit_timeline.py:264-270`, `hook_aware.py:459-490` `[lead]`.

**Remedy.** `encode.py` (`Profile.MEZZANINE` / `Profile.FINAL`, one `run()`), `jsonio.py`,
`proc.py`, `retry.py`. `overlay/overlay_encode.py` already is the encoder-policy module in
spirit — make it the only one.

### ⬜ CR-05 — Untyped boundaries: 8 dataclasses against 251 `: dict` parameters — major `[verified]` **(OPEN)**

- `@dataclass` appears **8** times in the repo (`shorts/…` excluded: `faceit/transcribe_comms.py`,
  `hltv/match_listener.py`, `overlay/overlay_utilcams.py`, `overlay/victim_rewind.py`,
  `pov/render_version_check.py`, `pov/round_windows.py`, `pov/verify_pov.py`).
- **251** `: dict` parameter annotations; **84** `dict`-returning functions.
- `pipeline.load_state()` returns `{"step": 1, "data": {}}`; **94 distinct**
  `state["data"][...]` keys are *referenced* across `pipeline.py`, of which only **12** are
  *written* there — the rest are written by other steps into the same blob. No single module
  can validate that shape.
- `self.meta` carries ~28 further untyped keys `[lead]`.
- Timelines, backlog cards, sidecars and meta payloads are all loose dicts with implicit
  schemas (`candidate.get("raw_star_bonus")`-style key fishing throughout
  `build_edit_timeline.py:1231+`). Note the repo *already has* a pydantic models module —
  `scripts/models.py:23,61,82,92` (`MatchInfo`, `DownloadResult`, `DemoRecord`,
  `PlayerAccount`) — used for acquisition only. The rendering pipeline does not use it, so the
  typed-model pattern exists but was never extended to the records that actually move through
  the pipeline.

**Remedy.** Dataclasses for `PipelineState`, `RunMeta`, `BacklogCard`, `TimelineSidecar`,
`ShortCandidate`; validate at boundaries. This also deletes the ad-hoc fallbacks that exist
only because the invariant is unstated (e.g. `demand_gate.py:110`).

### ⬜ CR-06 — Pipeline double-implements its steps — major `[verified]` **(OPEN)**

`pipeline.py` **both** shells out to sibling scripts (`_run_py` `:719-726`,
`subprocess.run([PY] + args)`) **and** imports them as functions
(`from overlay.overlay_pov import run_overlay` `:1454-1467`). Nine `_run_py` sites
(`:908,1336,1488,1520,1681,1690,1704,1799,1843`). Small sample of the relative-path,
cwd-dependent argv:

```python
pipeline.py:849   "scripts/pov/render_pov.py", str(self.demo_path), self.steam_id,
pipeline.py:1310  concat_args = ["scripts/pov/concat_rounds.py", str(self.render_dir)]
pipeline.py:1520  self._run_py(["scripts/pov/generate_outro.py", str(video)], timeout=120)
```

which only work because `pipeline.py:63` runs `os.chdir(str(PROJECT_ROOT))` **at import time**.

Resume/staleness decisions are re-implemented per step, expressed as five different size
thresholds for the same concept:

```
pipeline.py:565    > 100 * 1024 * 1024     # combined.mp4 considered complete
pipeline.py:972    > 1_000_000             # copy considered complete
pipeline.py:1273   > 1_000_000             # overlay copy considered complete
pipeline.py:1371   > 100_000               # overlay video considered complete
pipeline.py:1417   < 100_000               # combined.mp4 considered missing/empty
pipeline.py:1503   > 100_000               # voice mix considered complete
pipeline.py:1711   < 1_000_000             # hook mp4 considered present
pipeline.py:1721   < 1_000_000             # hook prepend considered complete
pipeline.py:1846   < 1_000_000             # intro prepend considered complete
plus pipeline.py:1276  target.stat().st_size >= overlay.stat().st_size * 0.95
```

Plus ~10 distinct ad-hoc "done/stale" checks (`_auto_skip_completed` `:545`, `:686`,
`_copy_video_to_youtube` `:960`, `_copy_overlay_result_to_youtube` `:1240-1262`,
`step_overlay` `:1355-1385` and `:1407`, `_find_overlay_video` `~:2005`, `_prepend_hook`
`:1720-1745`, `_prepend_intro` `:1845-1857`, `step_concat` `:1290`, `_generate_thumbnail`
`:2150`) `[lead: line refs for the ~10 checks]`.

Non-atomic artifact pairs (crash leaves inconsistent state) `[lead]`:
`:1288-1298` (video, then sidecar), `:1430-1438` (hardlink video, then copy sidecar),
and in both prepends: `out.replace(video)` before `sidecar.write_text`.

**Remedy.** Each step exposes `run(ctx)`; the pipeline calls it in-process; `main()` only
parses arguments. One `ResumePolicy` owning per-step artifact manifests and thresholds.
Delete `_run_py`, the argv marshalling, and the stdout string-matching used for control flow.

### ✅ CR-07 — Canonical `settings` paths bypassed by ten modules — minor `[verified]` **(DONE 2026-09-27)**

`scripts/config.py` already owns `csdm_cmd`, `ffmpeg_exe`, `ffprobe_exe`, `cs2_cfg_dir`,
`cs2util_root`. The same absolute paths are re-typed in:

- `scripts/faceit/intro_prepend.py:55` (game cfg dir)
- `scripts/misc/steam_mode.py:26` (steam.exe)
- `scripts/pov/render_version_check.py:47,50` (csdm exe, `steam.inf`)
- `scripts/shorts/render_pending_shorts.py:43` (ffprobe)
- `scripts/upload/upload_pending_shorts.py:277` (CS2UtilArchive root)
- `scripts/upload/upload_youtube_shorts.py:38` (CS2UtilArchive scripts root)
- `scripts/faceit/mix_team_voice.py:65` (conda site-packages path)
- plus personal absolute-path defaults inside `config.py` itself (`:34,38,41,44,47`).

`scripts/shorts/popular_events.py` (33 lines) and `scripts/upload/assign_playlist.py` (13 lines)
are in the same category: hardcoded data/policy that belongs in settings or a data file.

### ✅ CR-08 — Dead weight — minor `[verified]` **(DONE 2026-09-27)**

- Tracked dead file: `scripts/highlights/build_kill_timeline.py.deprecated`.
- Dead code shipped in a live module: `scripts/shorts/ml_common.py:26-29` contains
  `mid = cuts[k + 1] if False else None; _ = mid`, plus an unused local.
- **Shadowed duplicate method in a 2443-line class** — `@staticmethod _probe_duration` is
  defined twice in the same class body: `pipeline.py:1087` and `:1876`. Python keeps the
  later definition, so **`:1087` is dead code**. Both call `settings.ffprobe_exe`, with
  different flag sets. A lint gate catches this for free; there is none (CR-13).
- `scripts/misc/` holds **28 tracked files**, largely one-off golden-comparison and probe
  scripts (`_diff_check.py`, `_recover_rendered_73.py`, `compare_batch1_goal.py`,
  `regen_batch1_goal.py`, `dump_edit_segs.py`, `score_edit_vs_golden.py`, …). Also
  `scripts/pov/debug_search.py`, `debug_search2.py`, `scripts/shorts/sweep_models.py`.
- Untracked scratch adjacent to source: `.tmp/`, `tmp/`, `.pipeline/` each hold dozens of
  throwaway probe scripts; a stray `nul` artifact sits in the repo root.
- `scripts/pipeline_chain.py` is documented in `AGENTS.md` as a leftover manual helper, not
  used in production.

### ◐ CR-09 — Silent-failure culture — major `[verified count]` **(PARTLY DONE)**

- **371** `except Exception` across `scripts/`, `scrapers/`, `commands/`, `thumbnail/`;
  **91** of them immediately followed by `pass`.
- The repository's own documentation names an instance of this class: `overlay/victim_rewind.py`
  maps a failed mesh load to `None`, which reads as "line of sight open", so a missing
  collision mesh silently yields **zero** `insta_kill` detections. The hook builder warns about
  it rather than failing (root `AGENTS.md`, "Hook (POV Cold Open)" section).
- Worst instance found during this review: CR-10.

### ✅ CR-10 — HLAE process manager treats "measurement failed" as "process absent" — major `[verified]` **(DONE 2026-09-27)**

`scripts/hook_aware.py` conflates a failed measurement with a negative result — and states the
defect in its own docstring:

```python
:118  def _image_pids(image_name: str) -> set[int]:
          """PIDs for an image. Empty if none (or tasklist failed)."""
:137      except Exception:
              return set()

:115  def _process_running(image_name: str) -> bool:
          return bool(_image_pids(image_name))      # tasklist failure ⇒ "not running"

:161  def _afx_pids() -> set[int]:                  # discovers hooked CS2 (AfxHookSource2.dll)
:175      except Exception:
              return set()                          # tasklist failure ⇒ "no hooked CS2"

:144  def _taskkill_pid(pid: int) -> None:
:150      except Exception:
              pass                                  # kill failure silent
```

This is the liveness layer that decides whether to inject/record HLAE hooks and whether a
CS2/HLAE process still needs killing. A transient `tasklist` failure therefore reads as
"CS2 is gone" (hook misfires) or as "nothing to kill" (zombie process survives, and the next
render starts against a dirty environment). `docs/bugs/hlae-steam-online-hook.md` is an open,
unsolved bug about Steam-online HLAE inject/record flake; this belongs on that investigation's
shortlist as a candidate contributing mechanism.

**Remedy.** Distinguish the three outcomes (running / not running / unknown). Let the process
query raise or return an explicit `Unknown`, and make callers decide — never fold "could not
tell" into "absent".

### ✅ CR-11 — Cross-product circular import with a silent fallback that wipes wallbang detection — major `[verified]` **(DONE 2026-09-27)**

```python
highlights/build_action_timeline.py:178   from shorts.build_short_timeline import (...)
shorts/build_short_timeline.py:686        from highlights.build_action_timeline import _resolve_weapon_id
```

Two product packages import each other. The shorts side imports a **private** symbol
(`_resolve_weapon_id`) from inside a function — deferred specifically to survive the cycle —
and wraps it in a silent fallback:

```python
shorts/build_short_timeline.py:686-710
    try:
        import numpy as np
        from highlights.build_action_timeline import _resolve_weapon_id
        ...
        victim_weapon_map[key] = _resolve_weapon_id(int(val))
    except Exception:
        victim_weapon_map = {}
```

If that import fails (circularity, numpy unavailable, refactor), the victim-weapon map is
emptied without a warning. `_is_wallbang_rifle` (`:127`) is then handed empty weapon strings at
`:1150`, so **wallbang detection silently produces nothing**. Two weapon vocabularies
(CR-03) are why the private cross-import exists in the first place.

**Remedy.** One `weapon_rules` module owned by neither product; both import it. This removes the
cycle and the fallback.

### ✅ CR-12 — Live encoder drift: `assemble_reel` uses undocumented quantizers — major `[verified]` **(DONE 2026-09-27)**

```python
scripts/highlights/assemble_reel.py:425   "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "13",
scripts/highlights/assemble_reel.py:495   "-c:v", "h264_nvenc", "-preset", "p7", "-b:v", "0", "-cq", "14",
```

`docs/agents/rendering.md` defines exactly two encode profiles (mezzanine CQ10/8 at 200M;
final export CQ15 at 60M). `cq13` and `cq14` are neither. The reel is produced at an
undocumented quality, so the delivered bitstream silently diverges from the documented policy —
which is precisely the failure mode that 19 hand-typed encoder argument lists (CR-04) makes
inevitable.

### ✅ CR-13 — No lint gate, no CI, no `pyproject.toml` — major `[verified]` **(DONE 2026-09-27)**

- No `pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, `.ruff.toml`, `.flake8`,
  `.editorconfig`, `.pre-commit-config.yaml`, or `.github/`.
- Tests run only because `tests/conftest.py` inserts `scripts/` onto `sys.path`; 88 files
  under `tests/`.
- Consequences already realised: the shadowed duplicate method in CR-08, the undocumented
  quantizers in CR-12, and 91 silent `except Exception: pass` sites in CR-09.

**Remedy.** `pyproject.toml` (packaging + ruff + pytest config) and a CI workflow running
`ruff check` and `pytest`. This is the cheapest item in the whole report and the one that stops
the debt from re-accumulating.

### ✅ CR-14 — `insta_kill` mesh guard was permanently tripped — major `[verified]` **(DONE 2026-09-27)**

*Found during the CR-01 patch review (space-bunny), not in the original pass. Same class as CR-09
and CR-11, and the clearest instance of it.*

`pov/build_hook_timeline._map_mesh_available()` is the guard the hook docs describe as the
protection against silent `insta_kill` failure. It read:

```python
root = str(Path(settings.cs2util_root))
if root not in sys.path:
    sys.path.insert(0, root)
from render.map_collision import _grid_for_map
return _grid_for_map(map_name) is not None
except Exception:
    return False
```

The sibling's collision mesh lives at `<sibling>/scripts/render/map_collision.py`, but only the
sibling **repo root** was inserted. The import therefore raised, `except Exception: return False`
swallowed it, and the function reported "mesh unavailable" for every map — the exact condition that
`victim_rewind` maps to "line of sight always open", which pushes every time-to-kill past the 0.5s
cap and yields **zero** `insta_kill` detections. It worked only when some earlier code path in the
same process had already put the sibling's `scripts/` directory on `sys.path`; standalone
invocations never could.

**Fixed** in CR-01 by calling `overlay/_common.prefer_cs2util_scripts()`, which inserts both sibling
paths. Verified afterwards: `render.map_collision` imports and `de_mirage`, `de_nuke` and `de_dust2`
all return a collision grid.

---

## Code-judo moves (ranked)

1. **Make it a package** (CR-01, CR-13). Deletes `_pathsetup.py`, 140 `sys.path` sites,
   dual import identity, and the module-shadowing bug class — the class that has already
   forced two production workarounds. Add `pyproject.toml` + ruff + CI in the same pass.
2. **Detector registry for shorts** (CR-02). `RoundContext` dataclass + `DETECTORS` table, one
   module per detector. `detect_shorts()` 832 lines → ~50; deletes ~600 lines and five copies
   of the `kills_by_round` / `by_attacker` scan. Honest caveats: detectors need different
   granularities (per-attacker for 4K/perfect, per-kill for wallbang/knife/flick, cross-round
   alive-state for clutch/1v3, event-driven for defuse), and the global post-passes
   (`:1600` pro gate, `:1617` clutch-overlap dedup, `:1634` flick fold) are keyed on
   `short_type` strings, so they must remain a separate pipeline stage.
3. **One shared `demo_facts` + `weapon_rules` + `round_windows` + `demand` module** (CR-03,
   CR-11). Deletes four `_round_for_tick` copies, the duplicated winner fork, the duplicated
   tail-trim implementation, three duplicated constants, the dual weapon vocabulary, the
   duplicated demand table, and the cross-product cycle.
4. **One encoder / jsonio / proc / retry layer** (CR-04, CR-12). Collapses 19 NVENC sites,
   70 read + ~44 write JSON sites, 12 sibling-launch copies, and makes the documented encode
   policy true again.
5. **Pipeline in-process steps + one `ResumePolicy`** (CR-06). Deletes `_run_py`, argv
   marshalling, stdout outcome parsing, ~10 ad-hoc staleness checks and 5 magic size
   thresholds.
6. **Typed state and records** (CR-05). `PipelineState` / `RunMeta` / `Sidecar` (with
   `shift(dur)`) / `BacklogCard` / `ShortCandidate`; kills 94 string keys, the duplicated
   sidecar-shift block, and the duplicate `_probe_duration`.

Deletion estimates from the audits, summed conservatively: **~1500–1900 lines** and five
categories of latent bug.

---

## Change batches

### Batch A — mechanical, no behaviour change

- [ ] A1. `pyproject.toml` + `cs2archive` package: subpackages mirroring buckets, `__init__.py`
      everywhere, `[project.scripts]` entrypoints.
- [ ] A2. Delete `scripts/_pathsetup.py` and all 140 `sys.path` sites; fix `main.py:22-25` and
      `tests/conftest.py:11`.
- [ ] A3. Move shared HLAE/CSDM infrastructure (`hook_aware.py`) into a real layer; update its
      six importers.
- [ ] A4. Add `ruff` config + `.github/workflows/ci.yml` running `ruff check` and `pytest` (CR-13).
- [ ] A5. Route all hardcoded tool paths through `settings` (CR-07 list).
- [ ] A6. Delete `scripts/highlights/build_kill_timeline.py.deprecated`, the dead
      `ml_common.time_folds` branch, `tmp/` probe scripts, `scripts/pov/debug_search*.py`,
      the `scripts/misc/` one-offs intended as scratch, the stray root `nul`.
- [ ] A7. Delete the dead shadowed `_probe_duration` at `pipeline.py:1087` (CR-08).

### Batch B — extract canonical helpers

- [ ] B1. `encode.py` (two documented profiles) + rewrite the 19 NVENC sites; fix
      `assemble_reel` CQ13/CQ14 drift (CR-04, CR-12).
- [ ] B2. `jsonio.py`, `proc.py`, `retry.py`; rewrite their call sites (CR-04).
- [ ] B3. One `demand` module: move the fallback table to `.data/*.json`, delete the
      `scrape_notable` re-export and `demand_gate._load_payload` (CR-03).
- [ ] B4. One ML trainer shared by `fit_clip_weights` / `fit_pov_weights` (CR-03).
- [ ] B5. One `round_windows` API used by both POV and highlights; delete the duplicated
      constants and the second tail-trim implementation (CR-03).

### Batch C — structural

- [ ] C1. `RoundContext` + detector registry: decompose `detect_shorts()` (CR-02).
- [ ] C2. Decompose `run_overlay()` into phases; introduce `OverlayPolicy` for the
      freeze/keyboard/voice/shade/pip flags (CR-02).
- [ ] C3. Split `scrapers/faceit.py` into `client` / `downloads` / `browser` / `fs_watch` (CR-02).
- [ ] C4. Split `build_edit_timeline._fix_edit_timeline` / `_ensure_round_closers`; move the
      LLM client (`:42-47`, `:360-447`) into a shared module (CR-02, CR-04).
- [ ] C5. Pipeline: steps become in-process `run(ctx)` callables; one `ResumePolicy`; delete
      `_run_py` and stdout pattern-matching (CR-06).
- [ ] C6. Typed `PipelineState` / `RunMeta` / `Sidecar` / `BacklogCard` / `ShortCandidate`;
      make artifact writes atomic (video+sidecar pairs) (CR-05, CR-06).
- [ ] C7. Fix `hook_aware` process liveness to distinguish running / not-running / unknown (CR-10).
- [ ] C8. Break the cross-product `shorts` ↔ `highlights` cycle behind a shared
      `weapon_rules` module, removing the silent wallbang-wipe fallback (CR-11).

### Batch D — policy

- [ ] D1. Replace the 91 `except Exception: pass` sites with narrow exception types plus a
      logged warning (CR-09).
- [ ] D2. Track `except Exception` count and file-size as review metrics; require an ADR for any
      file crossing 1000 lines.

---

## Guardrails (must not regress while executing any batch)

- **Resume contract:** `.pipeline/{run_id}.json` plus artifact-based skipping must keep working.
  Never delete `renders/pov-*` before upload — purge is post-upload by design
  (`upload_pending.py`), and the render dir is the re-overlay/re-scale repair path.
- **Never clean up `demos/avatars/`** — persistent cache shared across all matches.
- **Never delete `.dem` files or `demos/` directories**, and never delete inside
  `CS2UtilArchive/demos/extracted` (a junction onto `demos/hltv`).
- **Never restart or offline-toggle Steam.** Pipeline `RENDER_STEAM_NOT_RUNNING` is the real check.
- **Encode split is load-bearing:** mezzanine (CQ10/8, 200M cap) through the pipeline, final
  export (CQ15, 60M) as the delivered bitstream — the 1440p VP9 trick depends on it. B1 must
  preserve exactly two profiles, not invent a third.
- **`--skip-failed-rounds` stays off by default** — it silently drops rounds.

---

## Method and confidence

**Method.** AST metrics sweep (file size, function count, longest function, `if` density,
maximum indent depth) over all `.py` files outside `.cache/`, `tools/`, `exports/`,
`grafipy-out/`; targeted greps for duplication, `sys.path` sites, exception density, typed-boundary
density and hardcoded paths; four read-only sub-agent audits (one per subsystem: `pipeline.py`,
timeline builders, overlay layer, cross-cutting duplication); then direct re-verification of
every material claim by the reviewing agent.

**Verification legend.** `[verified]` = re-checked against source during this review.
`[lead]` = reported by a sub-agent and not independently re-checked. `[refuted]` = claimed and
proven false.

**Delegate reliability.** Three of four sub-agent audits produced accurate, line-referenced
work. The timeline-builders audit contained one **`[refuted]`** claim: a function named
`_tier_helpers` at `build_short_timeline.py:174-187`, described as returning `None` on
`ImportError` and thereby disabling wallbang detection. **No such function exists**
(`grep -n "_tier_helpers"` returns nothing), and the cited line range is unrelated code. The
underlying concern the claim pointed at is real, but the mechanism is at `:686-710` (see
CR-11). That audit also **under-counted** `_round_for_tick` copies (four, not three — it
missed `pov/build_hook_timeline.py:245`). Both errors were caught before publication; the
refuted claim is recorded here so it is not re-raised. Treat `[lead]` items as hypotheses.

**Not in scope.** Runtime correctness, security posture (secret handling was checked and is
correct: `.env`, `token_youtube*.json`, `client_secret.json`, `faceit_pros.json` and browser
sessions are all gitignored), performance characteristics, output quality, and the contents of
`docs/bugs/`.

**Supersedes.** Nothing. This is the first review under `docs/reviews/`.
