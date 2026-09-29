# Council log — review patching

Running record of every decision taken while patching `2026-09-27-thermo-nuclear-code-review.md`.
One section per review item.

## Protocol

**Members** (Herdr-managed agents, persistent sessions, kept alive across all items so context and
prompt cache accumulate):

| Member | Kind | Model | Effort | Pane |
|---|---|---|---|---|
| `space-bunny` | pi | `openrouter/stealth/space-bunny-alpha` | medium | `w2:p2` |
| `muse` | opencode | `opencode/muse-spark-1.3-contributor-free` (`--auto`) | medium | `w2:p5` |

**Rules**

1. Every decision goes through **both** members. The parent (the reviewing agent) does not decide
   alone.
2. Members receive **identical briefs**. The parent first gathers the evidence, then states it in the
   brief, so both members reason from the same facts rather than exploring differently.
3. Members answer in a **fixed format** with a word cap, and must supply an *objection* per decision
   even when agreeing — agreement without a stated objection is not useful output.
4. Split outcomes get a **tie-break round**: the parent supplies the missing fact (or the competing
   objection) and asks for final positions. Unanimity after a tie-break round is recorded as such.
5. The parent **corrects its own premises** in the brief when new evidence arrives, and says which
   earlier claim changed. Both members changed position twice under this rule in CR-01, which is the
   signal that it works.
6. Decisions are logged with the vote, the reason and the objection. A parent tie-break, if ever
   used, must be recorded as a parent tie-break with the evidence it rests on.
7. No member edits files during a decision round ("decisions only"). Patching is the parent's job;
   reviewing the patch is the members' job.
8. Members are reachable via `herdr agent prompt <name> "<brief>" --wait --timeout <ms>`, read via
   `herdr agent read <name> --source recent-unwrapped --lines <n>`.

**Operational note:** write briefs to a file (`.tmp/council-brief-*.md`) and pass the contents.
Inlining a brief in a double-quoted shell variable lets bash execute backticked content —
round 4 of CR-01 went out with `_pathsetup` silently substituted. Both members still answered
correctly, but the defect is recorded here.

---

## CR-01 — `sys.path` bootstrap / no package

Four rounds, 2026-09-27. Outcome: **consensus on all nine decisions.**

### Decisions

| Id | Decision | Vote | Notes |
|---|---|---|---|
| D0 | Re-verify only the evidence inside the 15 dirty files; trust the structural counts already re-measured | **A-narrow**, unanimous | Both changed from round 1 (space-bunny had B, muse had A) |
| D1 | `cs2archive` package name; fold **only `scripts/`** in pass 1; leave `commands/`, `scrapers/`, `thumbnail/` top-level | **(a)**, unanimous | Blast-radius control; basename collisions move in pass 2 |
| D2 | Console scripts + module invocation, **with** thin compat shims at the old paths | **WITH shims**, unanimous | Keeps the listener and external callers running |
| D3 | `pipeline_cmd` becomes path-free (`python -m …`); rewrite the 545 existing cards | **free**, unanimous | Both changed from round 1 after FACT 1 landed |
| D4 | Edit `run_refresh_stars.ps1` **body in place**; no task re-registration | **iii**, unanimous (muse changed from i) | Task invokes the ps1 by path, so body edits need no re-register |
| D5 | Shims are **hard-warn + delete**, not silent redirects | unanimous | Sunset trigger defined below |
| D6 | Rewrite all 44 test imports to `cs2archive.*`, install editable, delete the `tests/conftest.py` sys.path hack | **(a)**, unanimous | A tests-only compat path would recreate the double-importability CR-01 exists to kill |
| D7 | Update `AGENTS.md` path references now; defer the other 144 doc refs to a doc pass | **(b)**, unanimous | AGENTS.md is the agent-facing contract |
| D8 | Delete all 69 `_pathsetup.ensure()` call sites **and** the module | **(a)**, unanimous | muse caveat: first confirm nothing relies on the import side effect (dynamic loading) |

### Evidence gathered by the parent

- **The reviewed evidence survives the baseline move.** At HEAD `7f4b3aa` + the dirty worktree:
  `scripts/__init__.py` absent (also absent in the `HEAD` tree), only `hltv/`, `overlay/`, `shorts/`
  have `__init__.py`, exactly **140** `sys.path.insert/append` sites, all five duplicate basename
  pairs intact (`commands|thumbnail utils.py`; `commands|scrapers faceit.py`, `hltv.py`, `ratings.py`,
  `trending.py`), `hook_aware.py` still at `scripts/` root. 44 of 85 test files import flat script
  modules; 27 import `_pathsetup`; 69 `ensure()` call sites; 175 doc path references.
- **FACT 1 — `pipeline_cmd` is inert.** Nothing executes it. Written by
  `scripts/_backlog_common.py:128`, string-patched by `scripts/faceit/migrate_backlog_dates.py:10`,
  asserted in `tests/test_quality_seams.py:79-91`; otherwise only read by a human. All 545 cards
  lack an `upload_status` field entirely, and only 2 were touched in the last 7 days.
  **Amendment:** the review report's implicit "544 cards are a runtime dependency" framing is wrong;
  the card field is documentation/ergonomics.
- **FACT 2 — the real path dependencies are code + one out-of-repo consumer.** The listener builds
  absolute paths in code (`scripts/hltv/match_listener.py:789`, `:806`, `:880`), plus 12+ sibling
  spawn sites (CR-04). The only consumer outside the repo tree is Windows task
  `\CS2ArchiveStarRefresh` (next run 28/09/2026 12:00, `Ready`), whose action is
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\Projects\CS2Archive\scripts\hltv\run_refresh_stars.ps1"`.
  That wrapper independently re-implements the bootstrap (`$env:PYTHONPATH = "$Root\scripts;$Root"`)
  and hardcodes the interpreter.
  **Amendment:** the review report does not mention this at all — CR-01 is *wider* than written
  (a live scheduled task depends on the old layout) and *narrower* than written (cards do not).

### Guardrails established

- **`scripts/hltv/run_refresh_stars.ps1` must never be moved or renamed.** The task pins its path.
  Only its body may change.
- Shims exist only for the external/human surface, and must print
  `DEPRECATED <old path> -> python -m ...` to stderr.
  **Sunset trigger:** the shims are deleted in pass 2 once a grep finds no `DEPRECATED` lines in
  listener logs and no remaining `ROOT / "scripts/..."` literals in the codebase.
- Confirm no dynamic-import side effect depends on `_pathsetup` before deleting it — note that
  `scripts/pov/pipeline.py:127-135` already uses `importlib.util.spec_from_file_location`, and
  `scripts/pov/create_backlog.py:368` generates code that imports `scripts.overlay._common`.
- `overlay/_common.py`'s `sys.modules["scripts"]` purge (for the CS2UtilArchive sibling checkout)
  is the highest-risk interaction: a shim at that path can **re-arm** the shadowing loop. Both
  members raised this in both rounds.

### Patch plan

See the parent's message and the "CR-01 patch plan" section of the working notes; it is applied in
this order (muse's ordering, which the parent adopted):

1. Scaffold `cs2archive/` + `pyproject.toml`, editable install.
2. Rewrite `scripts/` imports to package-relative.
3. Delete the 69 `ensure()` sites and `_pathsetup.py`.
4. Rewrite the 44 test imports; delete the conftest hack.
5. Hard-warn shims at the old paths; edit the ps1 body; rewrite `pipeline_cmd` + migrate 545 cards.
6. Update `AGENTS.md`.
7. Full test run + smoke checks.

Status: **paused mid-implementation at the owner's request.** Phases 1-3 and part of 5 are done;
see "Implementation status" below for exactly what landed and what remains.

---

## CR-01 implementation status (paused 2026-09-27)

### Landed

- **`cs2archive/` package created** (168 files + 10 dirs moved), subpackages mirroring the old
  buckets (`pov overlay faceit highlights hltv upload hf shorts misc`) plus a new `render/` holding
  `hook_aware.py` (council 1b). Depth invariant held: `cs2archive/<bucket>/x.py` has the same
  nesting depth as `scripts/<bucket>/x.py`, so all ~140 `parents[1]`/`parents[2]` PROJECT_ROOT
  computations kept working untouched.
- **`pyproject.toml`** (setuptools, `[project.scripts]` for the 10 modules that expose `main()`,
  `commands*/scrapers*/thumbnail*` declared so they stay importable flat). Installed with
  `pip install -e . --no-deps` deliberately, so pip did not re-resolve the working conda env.
- **583 imports rewritten** in total: 390 statement rewrites (phase B) + 138 bare bucket-module
  imports the first pass missed (the old bootstrap put every bucket dir on `sys.path`, so
  `from round_windows import ...` worked bare) + 39 quoted string-target rewrites (phase B2).
- **`_pathsetup.py` deleted**; 229 `sys.path` mutations, 93 `_pathsetup` imports and 86 `ensure()`
  calls removed; `X = ensure()` rewritten to `parents[N]`.
- **`prefer_cs2util_scripts()` reworked** to drop only a cached `scripts` module that did not come
  from the sibling checkout (muse's amendment), instead of the old `hasattr` heuristic.
- **`pipeline._minimize_cs2` importlib workaround deleted** — it existed only to dodge the
  shadowed `cs2_minimizer`; it is now a plain import (closes a CR-08 item as a side effect).
- **193 path literals repointed** from `scripts/...` to `cs2archive/...`, plus the generated
  `_ingest_worker.py` source string, `upload_pending`/`upload_pending_shorts` sibling-spawn paths,
  `match_listener._child_env()`, and the `misc/*` fixture paths.
- **`scripts/hltv/run_refresh_stars.ps1` body updated** to `-m cs2archive.hltv.refresh_stars` with
  the `$Root\scripts` PYTHONPATH entry removed. Its **path is unchanged** — the Windows task
  `\CS2ArchiveStarRefresh` pins `-File "D:\Projects\CS2Archive\scripts\hltv\run_refresh_stars.ps1"`.

### Verification

| Gate | Result |
|---|---|
| `compileall` over cs2archive/scrapers/commands/thumbnail/tests | clean |
| import every module in the package | 158 OK; 8 failures all pre-existing (those modules read artifacts / sys.argv / hit an API at import time) |
| every rewritten import target resolves inside the package | **0 unresolved** |
| bare imports that still refer to a bucket module | **0** |
| legacy `_pathsetup` code references | **0** |
| remaining `sys.path` mutations | **6 — all CS2UtilArchive sibling interop**, each inspected individually |
| flat shadowing gone | `import faceit_names` raises ModuleNotFoundError |

### Test suite: correct baseline matters

A naive comparison against `HEAD` is wrong: the true pre-refactor state is **HEAD + the owner's 15
uncommitted files**, and one failure (`test_crosshair_resolve:test_hook_build_config_uses_shared_system`)
is caused by that WIP (it changed `render_hook` to call `resolve_crosshair_cvars` via a bound import,
which a string-target monkeypatch cannot intercept). The baseline was measured by checking out HEAD
into a throwaway worktree and applying the captured WIP patch.

| Run | Result |
|---|---|
| baseline (HEAD + WIP) | 21 failed, 741 passed, 4 skipped, 2 errors |
| after CR-01 refactor | **16 failed, 747 passed, 3 skipped, 2 errors** |
| **new failures caused by CR-01** | **0** |
| baseline failures that now pass | 5 |

### Outstanding CR-01 work (not started)

*(superseded — see "Review round" below)*

1. ~~Shims at the old paths~~ — done (28 shims).
2. ~~`pipeline_cmd` → module form + migrate the 545 cards~~ — done.
3. ~~`AGENTS.md`~~ — done (+ `gotchas.md`).
4. ~~`tests/conftest.py` sys.path hack removal~~ — already gone.
5. ~~Verification owed~~ — done (see below).
6. CR-13 (lint + CI) — **still open**; `pyproject.toml` carries packaging only.

---

## CR-01 review round (council)

Both members reviewed the patch. **space-bunny: approve-with-changes. muse: approve-with-changes.**
Both accepted the DEV-1 scope call (no `scripts.<bucket>` namespace shims). muse asked me to
root-cause the 5 unexplained test flips rather than accept them.

### Defects they found, and what happened to each

| # | Found by | Defect | Outcome |
|---|---|---|---|
| 1 | space-bunny | `pov/build_hook_timeline.py` `_map_mesh_available()` inserted only `settings.cs2util_root`, but the sibling's collision mesh is at `<sibling>/scripts/render/`. The import failed, `except Exception: return False` returned False, and the **documented insta_kill silent-failure guard was permanently tripped** | **Fixed** — now calls `prefer_cs2util_scripts()`. Verified: `render.map_collision` imports and `de_mirage`/`de_nuke`/`de_dust2` all load grids (`grid=True`). space-bunny thought no map loads a grid; with the path fixed they do, so the guard was tripped *and* the mesh was unavailable |
| 2 | space-bunny | `upload/upload_youtube_shorts.py` — my Phase D left a **remove-only `sys.path` loop** (the block collector did not recognise `while str(_p) in sys.path:` as a guard, so it deleted the `insert` and kept the `remove`). It silently stripped the sibling's scripts dir from `sys.path` | **Fixed** — loop and its now-dead `_SCRIPTS_DIR`/`_UPLOAD_DIR` deleted. Swept the whole repo: only one other `sys.path.remove` site exists (`_common.py`, a correct remove-then-insert) |
| 3 | muse | `cs2archive/highlights/build_kill_timeline.py.deprecated` still contained `sys.path.insert` + `from _pathsetup import ensure`, inside the new package | **Fixed** — file deleted (also a CR-08 dead-weight item) |
| 4 | muse | `cs2archive/hltv/run_match_listener.ps1` launched `scripts\hltv\match_listener.py` and set `PYTHONPATH="$Root\scripts;$Root"` — a package file pointing back at the shim path | **Fixed** — now `-m cs2archive.hltv.match_listener`, `PYTHONPATH` reduced to `$Root` |
| 5 | muse (cascade I then found) | `install_match_listener_task.ps1` registers a task pointing at `scripts\hltv\run_match_listener.ps1`, which **no longer exists** — the installer would have baked in a broken path | **Fixed** — points at `cs2archive\hltv\run_match_listener.ps1` |
| 6 | muse (cascade I then found) | `stop_match_listener.ps1` matched python processes by `match_listener\.py`; the `-m` form would no longer match, so the single-owner guard would stop finding listeners | **Fixed** — pattern relaxed to `match_listener`; doc example path corrected |
| 7 | space-bunny + muse | Dead comments still describing the removed bootstrap (`pov/concat_rounds.py`, `overlay/overlay_pov.py`, `tests/test_create_backlog.py`) | **Fixed** |
| 8 | space-bunny | `.cache/swift-demoui/smoke.py:14` does a flat `from hook_aware import …` — untracked scratch outside the repo tree, but proof that flat consumers exist beyond it | Recorded, not actioned (out-of-tree scratch). Shims cover the documented entry points only |

### The 5 test flips: root cause (muse's DEV-2)

My first baseline was **environment-degraded**: `git worktree` yields a fresh checkout, and
gitignored runtime state (`.data/`, `.cache/`) is absent there. Re-running the baseline with
`.data`, `.cache` and `.listener` linked in produced **15 failed, 748 passed, 3 skipped, 2 errors** —
**byte-identical to the refactor's set**. So the "5 baseline failures that now pass" were an
artifact of the missing state (e.g. `test_swift_demoui` failed on a missing `.cache` package file),
not a behaviour change in either direction. There are no unexplained flips.

### Verification completed for the review

| Item | Result |
|---|---|
| corrected baseline vs refactor | failure sets **identical**; 0 new, 0 flipped |
| every rewritten import target resolves | 0 unresolved |
| bare imports still pointing at a bucket module | 0 |
| non-interop `sys.path` mutations | 0 (5 sibling-interop kept) |
| `_pathsetup` references in code | 0 |
| `compileall` | clean |
| stale bootstrap commentary | 0 (new gate: `gate_stale_comments`) |
| `scripts/` literals in code | 34 remain, all intentional: 28 are the shims' own deprecation messages, 4 are CS2 `panorama/scripts/hud/` game paths, 1 is the sibling's `select_top_utils.py`, 1 is stale `.pipeline/` runtime state |
| sibling interop from a clean interpreter, foreign cwd | `render.map_collision` → CS2UtilArchive; our overlay → ours; no clobber |
| listener dry-run **from a foreign cwd** | `run_match_listener.ps1 -DryRun -Once` ran the module, polled HLTV, printed `[poll] 0 notable completed event match(es)` — wrapper is cwd-independent |
| no production listener disturbed | verified no `match_listener` python process was running before the dry-run (the wrapper has a kill-guard); `.listener/hltv.lock` is a stale 0-byte file |

### Follow-ups that cannot be done today

- **Post-run check of `\CS2ArchiveStarRefresh`** (muse). Its next run is 2026-09-28 12:00; the
  transcript at `.listener/stars.log` should be inspected after it fires to confirm the ps1 body
  change works in the scheduler, not just by hand.
- CR-13 (lint + CI) remains open.
- The other ~144 doc path references stay deferred (D7).
- Shim sunset: delete the 28 shims once no `DEPRECATED` lines appear in logs and gate G reports no
  remaining code literals.

### Process lessons recorded

- **Inline heredocs corrupt backslashes and execute backticks.** Two separate defects came from
  this: a mangled round-4 brief and a broken diff script. Write scripts/briefs with a file write,
  never inside a double-quoted shell string.
- **Anchored regexes miss trailing comments.** `import scoring as _scoring  # noqa` and
  `ensure()  # noqa: E402` both survived the first passes and broke 5 modules.
- **A name-only codemod rewrites the sibling repo's modules too.** 28 imports such as
  `render.map_collision` / `rank_utils` / `demo_ids` are CS2UtilArchive's, not ours; they were
  restored because the target did not resolve inside our package. space-bunny predicted exactly
  this class.
- **Gates are what caught it**, not judgement: "every rewritten target must resolve" found the 28,
  and "is any bare import actually a bucket module?" found the 138.
- **Stale `.pyc` in `scripts/__pycache__`** survived the move and had to be deleted explicitly.

---

## CR-13 — lint gate + CI

Four rounds, 2026-09-27. Outcome: **consensus on every decision.** Implemented and verified.

### Decisions

| Id | Decision | Vote | Notes |
|---|---|---|---|
| D1 | Gate ruleset = `F` + `E9` (crash-level only) | **a**, unanimous | space-bunny added S110/BLE001 as an advisory report, never a gate |
| D2 | Fix crash-level findings; baseline the cosmetic ones with tokened waivers | **a**, unanimous | Plus a constraint both implied: **do not edit files the concurrent agent holds uncommitted** |
| D3 | Run ruff via `uvx ruff@<pinned>` — no conda-env changes | **a**, unanimous | "The conda env is production for renders; mutating it for a linter is a bad trade" |
| D4 | …resolved over 3 rounds, see below | **b-scoped**, unanimous | Reached only after new evidence |
| D5 | The 3 real defects become a new finding, not part of CR-13 | **new CR-15**, unanimous | Plus: the zero-risk dead-code fix lands now |
| Extra | Every waiver carries a `CR-<n>` token + `expires` date, enforced in CI | unanimous | space-bunny: "a suppression cannot outlive its ticket" |

### D4 took three rounds because the approved policy was wrong — and the evidence proved it

Round 1 proposed a blocking pytest gate diffed against a checked-in `tests/known_failures.txt`
(muse wanted the list; space-bunny wanted advisory-only and predicted the list would encode a
baseline "the other agent is still moving under you"). The synthesis was accepted and built — and
building it immediately refuted its premise:

- **Two back-to-back full runs, no code change:** 14 failed / 766 passed vs 15 failed / 765 passed.
  The only difference was `test_swift_demoui.py::test_mount_restores_exact_gameinfo_after_render_failure`,
  which reads the cached Swift package that render work rewrites.
- **Order dependence:** `test_overlay_extraction.py`'s two entries ERROR when the file runs alone and
  PASS inside the full suite.
- The list churned within 30 minutes of being written: 2 listed failures started passing, 1 new
  failure appeared.

So the decision went back to the council rather than being quietly relaxed (round 3), then a final
tie-break (round 4) on the one point that decided it: **`xfail(strict=False)` still runs the test and
reports XPASS, so a narrowly-scoped quarantine removes the false alarms without losing the gating
signal for the other ~763 tests.** Both members moved to `b-scoped`.

### Implementation, and two disclosures

- `pyproject.toml`: `[tool.ruff]` (select `F`,`E9`; `ignore` F401/F541/F841 with measured counts and
  reasons), five `per-file-ignores` waivers, `[tool.pytest.ini_options]`, and `[project.scripts]`
  from CR-01.
- `.github/scripts/check_waivers.py` — fails CI on a waiver with no `CR-<n>` token, no expiry, or an
  expired date. Audits both the ruff per-file-ignores and the pytest config section.
- `.github/scripts/check_known_failures.py` — runs (or reuses) pytest, parses FAILED/ERROR ids, and
  fails only on ids **not** in `tests/known_failures.txt`; the safe direction (listed but passing) is
  a warning, never a failure. Warns loudly if invoked with an interpreter that lacks the project deps
  or with `.data/` missing, because the baseline is not comparable in that case.
- `.github/workflows/ci.yml` — ruff blocking; waiver audit blocking; test job advisory on hosted
  runners with `CS2ARCHIVE_ENFORCE_TESTS=1` to make it blocking where the environment exists.

**Disclosure 1 — the mechanism for two of the three quarantines changed, with reason.** The council
approved `xfail(strict=False)` for all three unstable tests. On inspection, two of them
(`test_vs_ground_truth`, `test_velocity_yaw`) are **not tests at all**: they take `(demo, steam_id[,
round_num])` and are called by that file's own `main()` at lines 223-224. Their "errors" were pytest
mis-collection, so the root-cause fix is `collect_ignore_glob`, not a mark. Intent preserved
(bounded quarantine), mechanism corrected, deviation recorded here. The third got the approved
`xfail(strict=False)`.

**Disclosure 2 — the CI test job is advisory on hosted runners.** D4' assumed pytest could gate in
CI. It cannot: the suite needs `.data/`/`.cache/` (gitignored) plus pandas/numpy/demoparser2/moviepy/
cloakbrowser and the sibling checkout. The gate is therefore enforced locally and on a self-hosted
runner (`CS2ARCHIVE_ENFORCE_TESTS=1`), while hosted CI still runs the suite and prints the delta so
drift is visible. This is narrower than the approved decision and is flagged for the council's
review of this patch.

### Verification

| Gate | Result |
|---|---|
| `uvx ruff@0.16.9 check .` | **All checks passed** |
| `check_waivers.py` | 7 waivers checked, all tokened + unexpired |
| `check_known_failures.py` run A | observed 14, listed 14, new 0 → OK |
| `check_known_failures.py` run B | **identical** → determinism restored |
| `compileall` | clean |

### Side effects worth noting

- The lint gate found **two live NameErrors on its first run** (CR-15). `pov/render_pov.py:268-270` is
  the subtle one: a dict comprehension unpacks `(a, _b)` but the guard tests `b`, which the AST
  confirms is never bound in `_rename_sequence_files` — so the partial-match path raises.
- `cs2archive/shorts/ml_common.time_folds` was rewritten from `lo, mid = cuts[k], cuts[k+1] if False
  else None; _ = mid; hi = ...` to the equivalent two-liner, with a behaviour-equality check across
  n = 0, 1, 5, 17, 100, 1000 (identical tuples yielded).
- `cs2archive/shorts/pro_context.py` lost 8 lines of unreachable code after a `return`.

### CR-13 review round (council)

Both members reviewed the patch. **space-bunny: approve-with-changes. muse: approve-with-changes.**
Both **accepted both disclosures** — muse independently verified Disclosure 1 by reading
`tests/test_overlay_extraction.py:223-224` and confirming the two helpers are called with
`(demo, steam_id)`, so collection suppression is the root-cause fix rather than a mark.

They found **7 defects**, all fixed. The most serious was a genuine hole in my own design.

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | both | The **global `ignore = [F401, F541, F841]`** is by far the largest waiver (287 measured sites) and `check_waivers.py` never audited it — a fourth entry could be added silently with no ticket or expiry | Audit now checks the global ignore block too; 8 waiver sites covered (was 7) |
| 2 | space-bunny | The audit accepted **any** `CR-<n>`, including a ticket that does not exist | Tokens are now cross-checked against `docs/reviews/README.md`. It immediately caught my own dangling `CR-17` reference — proof it works |
| 3 | space-bunny | `check_known_failures.py` warned that the environment was incomparable and then **compared anyway** — on a hosted runner that yields ~100 false "NEW" failures, i.e. permanent red noise that gets ignored | Now prints `SKIPPED` and exits 0 when pandas/numpy are missing or `.data/` is absent; `--force` overrides |
| 4 | muse | `normalise()` stripped the `FAILED`/`ERROR` prefix, so a **status flip was invisible** — a known id becoming an ERROR (collection/fixture breakage) looked unchanged | Comparison is now keyed on `(STATUS, id)`; a flip fails the check. Verified with a synthetic flip |
| 5 | space-bunny | `python -m pip install -r requirements.txt pytest || true` in the workflow **removed the only signal** that the suite could not run | `|| true` dropped; the job's `continue-on-error` still keeps the build green while showing the reason |
| 6 | space-bunny | `line-length = 120` was **dead config** (E501 is not selected) | Documented as informational-only in the config |
| 7 | both | Five of the fourteen baselined failures are all `test_build_short_timeline.py` — **one root cause, not five** — so the baseline read as five unrelated silences | Grouped under the new **CR-17**; the file header now names the clusters instead of listing bare ids |

Also recorded from their reviews: scope the `render_pov.py` per-file `F821` waiver down to line-level
`# noqa` once CR-15 clears it (both members), and decide the `test_*` helper rename versus keeping
`collect_ignore_glob` under CR-16 (muse). Both notes are in the register.

**Things they tried to break and could not** (worth keeping): ANSI colour, Windows backslashes and
`ERROR` lines all parse correctly (space-bunny fed a synthetic log through `--reuse`: 3/3 ids right);
a multi-line array entry cannot bypass the waiver audit's comment tracking; pinning `ruff@0.16.9`
rather than a range is correct, since a range would drift the ruleset under the measured waivers.

**Negative tests I ran on the fixed tooling** (exit codes captured without a pipe, since `| tail`
masks them):

| Scenario | Expected | Observed |
|---|---|---|
| waiver injected with no token / no expiry | audit fails | audit fails, names both missing fields |
| dependency install missing / `.data/` absent | SKIP, exit 0 | `SKIPPED` + exit 0 |
| one failure removed from the baseline | exit 1 | exit 1, names the new failure |
| a baselined id flipped `ERROR -> FAILED` | exit 1 | exit 1, names the flip |
| normal run | exit 0 | exit 0 |

---

## CR-10 — "measurement failed" read as "process absent"

Two decision rounds, 2026-09-27. Consensus on every decision. Implemented; 16 tests; pending the
council's review of the patch.

### The defect

`hook_aware.py` returned an empty set / `False` when `tasklist` failed, so a failed *measurement* was
indistinguishable from a *negative result*. Two sites matter:

- `:567` the HLAE poll loop — `if _afx_pids():` was falsy on a failed query, so after
  `HOOK_INJECT_GRACE` it set `fail_reason = "no HLAE hook in 20s"` and aborted a render that may have
  been hooking correctly. This is the same failure family as the open
  `docs/bugs/hlae-steam-online-hook.md`.
- `:581` the ffmpeg delta — a failed query made `new_ffmpeg` False, which fed
  `missing_ffmpeg_after_hook()` and reported a working hook as failed.

### Decisions

| Id | Decision | Vote |
|---|---|---|
| D1 | `ProcessQuery` dataclass carrying `.known` + `.pids`, not `set | None` | **c**, unanimous — muse: "`None` is falsy, so `if _afx_pids():` would recreate the bug; the dataclass forces a `.known` check" |
| D2 | Unknown never sets `fail_reason`; cap consecutive unknowns (5) then abort with the **distinct** reason "could not determine process state" | **a**, unanimous |
| D3 | Retry a bounded 3× with backoff, then report UNVERIFIED, log loudly, **never claim "clean"** | synthesis, both — (c) terminates in exactly (a) |
| D4 | Three-state tests are required | unanimous |
| Extra | The 7th site (`_taskkill_pid`'s silent `except: pass`, which caused `kill_stale_processes`'s false "clean") gets a caller-visible status | unanimous |
| Extra | The 8th site becomes **CR-18**, with only a narrow guard inside CR-10 | unanimous |

### Sites fixed (14 call sites, plus 2 the council did not list)

| # | Site | Policy applied |
|---|---|---|
| 1 | `:567` poll loop (crux) | unknown never sets `fail_reason`; warn once; cap 5 unknowns then the distinct abort reason |
| 2 | `:581` ffmpeg delta + baseline capture | delta only computed when **both** sides are known; unknown suppresses the "no ffmpeg" verdicts |
| 3 | `kill_stale_processes` verification | unknown ⇒ never reported as clean; prints "could not VERIFY cleanup for …" |
| 4 | `kill_unhooked_cs2` | unknown ⇒ refuses to kill, says so explicitly |
| 5 | `_warn_rtss` advisory | unknown ⇒ warns "could not check", instead of staying silent |
| 6 | `overlay/swift_demoui._live_render_processes` | unknown ⇒ `<indeterminate>` entry so callers keep refusing to steal a mount they cannot rule out |
| 7 | `_taskkill_pid` | returns a status; every caller now reports a failed kill |
| 8 | `kill_stale_processes` blanket kill | narrow guard: never blanket-kill an image that could not be observed (CR-18 for the real fix) |
| 9 | `_await_no_cs2` — **found by me, not the council** | its own docstring describes the corpse-poisoning loop; unknown used to return True ("dead"). Now waits, then refuses to launch with a distinct warning |
| 10 | `_clear_all_cs2` (new helper) | replaced three duplicated blanket kills; unknown clears nothing and says so |

### Two disclosures

- **The 9th site.** Neither member listed `_await_no_cs2`, whose docstring explicitly describes the
  deterministic poisoning that treating unknown-as-dead walks into. Fixed in the same pass.
- **A better outcome than approved, on CR-16.** While validating, the two intermittent
  `test_swift_demoui.py` tests turned out to be **coupling to real ambient process state, not flakes**:
  one asserts the "session already active" refusal, which only fires when a live renderer is
  detected. The approved plan was to quarantine them with `xfail(strict=False)`; instead each test now
  pins `_live_render_processes` to state its own intent. Both are real tests again — 9 passed, three
  consecutive runs, and the whole suite is deterministic across two full runs (14 failed / 778 passed,
  identical sets). The quarantine now covers only `test_overlay_extraction.py`, and the register's
  CR-16 row is downgraded accordingly.

### Verification

| Check | Result |
|---|---|
| `tests/test_hook_aware_vanilla.py` | 16 passed (was 4) |
| `tests/test_swift_demoui.py` ×3 | 9 passed each time, no xfail |
| full suite ×2 | 14 failed / 778 passed / 3 skipped, **identical sets**, 0 errors, 0 xfail |
| `uvx ruff check .` | clean |
| waiver audit | OK |
| known-failures delta | OK — no new failures, no status flips |

### CR-10 review round (council)

Both members: **approve-with-changes**, and both **accepted both disclosures** — DEV-1 ("`_await_no_cs2`
was the worst of the lot: an unknown query returned 'cs2 is dead' into a function whose own docstring
describes the corpse-poisoning loop") and DEV-2 ("pinning `_live_render_processes` is strictly better
than the xfail I approved"). They found **6 defects**, all fixed.

| # | Found by | Defect | Fix |
|---|---|---|---|
| 1 | space-bunny | **Real hole**: `check_waivers.py` did not audit `tests/conftest.py`, yet `pyproject.toml` claimed "check_waivers.py audits it there". The CR-16 waiver had silently become the only unaudited, unexpiring waiver in the repo — and the old pytest-section check matched nothing while still counting as checked | Audit now follows the waiver to the conftest (8 sites). Negative test: stripping the token/expiry from `conftest.py` makes the audit fail |
| 2 | muse | `ffmpeg_delta_unknown` was **set once and never cleared**, so one transient unknown permanently suppressed every "no ffmpeg" verdict for that attempt | Recomputed every iteration, and explicitly cleared on a known delta |
| 3 | muse + space-bunny | `unknown_polls` reset only on `present`, not on a known-**absent** reading, so the "N polls in a row" cap actually counted **cumulative** unknowns — five unrelated transient failures spread across a long render would abort it | Reset on any known reading. A known-absent reading is just as conclusive as a known-present one |
| 4 | space-bunny | No test covered the cap or the reset — the crux of CR-10 was untestable because it lived inline in a 700-line function | Extracted `classify_hook_poll()` (pure) + `HookPollVerdict`; 7 new tests cover the cap, both reset paths, the grace boundary and first-sighting |
| 5 | space-bunny | `swift_demoui._live_render_processes` docstring still said "empty when none" while it can now return `<indeterminate>` | Docstring corrected: the result is never a pure pid list |
| 6 | space-bunny | The accepted trade-off (a persistently-failing `tasklist` now runs ~6 min to fail instead of failing fast) was not written down anywhere | Documented in `docs/bugs/hlae-steam-online-hook.md` under a dated CR-10 section |

muse also ruled that the `swift_demoui` touch is "minimal, safe-direction, and outside the concurrent
set — keep", and that the CR-18 boundary is right: the no-blind-kill guard and pid logging belong in
CR-10, child-pid reaping does not.

### CR-10 final state

| Check | Result |
|---|---|
| `tests/test_hook_aware_vanilla.py` | **23 passed** (4 before) |
| full suite | 14 failed / **785 passed** / 3 skipped — 0 errors, 0 xfail, deterministic |
| `uvx ruff check .` | clean |
| waiver audit | OK — 8 sites, all tokened + unexpired, register-cross-checked |
| known-failures delta | OK — no new failures, no status flips |

---

## Batch 2 — CR-11, CR-12, CR-07, CR-08, CR-09 (one decision round, one implementation pass)

The owner asked for throughput, so these five went through **one** combined decision round (10
decisions) instead of five. Consensus on all ten; one split reconciled on the criterion.

| Id | Decision | Vote |
|---|---|---|
| CR-11 a | Shared weapon knowledge in `cs2archive/weapons.py` at package root (like `scoring.py`) | unanimous — space-bunny: "a `shared/` package for a single consumer pair is sprawl" |
| CR-11 b | Unify the two vocabularies **now**, not "move as-is" | unanimous — "the cycle exists because of split vocabularies; moving as-is preserves it" |
| CR-11 c | Module-scope import, failure is loud | unanimous |
| CR-12 a | `assemble_reel` takes the documented FINAL profile (CQ15/60M) | unanimous — "reel.mp4 is an upload artifact" |
| CR-12 b | Fix the drift here; let CR-04 extract the encoder later | unanimous |
| CR-07 a | csdm/ffprobe through settings; `steam.exe` a new field; opuslib derived | split by item, reconciled |
| CR-07 b | Keep the `config.py` defaults as overridable documentation | unanimous — "single-operator repo" |
| CR-08 a | Delete the one-offs — **filtered by muse's criterion** (no import, no doc/test ref) | reconciled: space-bunny's set, muse's test |
| CR-09 a | S110/BLE001 as a **ratchet** (advisory, fails only above a ceiling) + hand-fix the worst | unanimous |
| CR-09 b | Kill/verify/render-decision sites first | unanimous — "each one is a false 'clean' or a false 'failed'" |

### What landed

- **CR-11**: `cs2archive/weapons.py` now owns `WEAPON_IDS` (id→name), `WEAPON_TIER` (name→tier),
  `JUNK_MELEE` and the predicates (`resolve_weapon_id`, `weapon_tier`, `is_wallbang_rifle`,
  `is_zeus`, `is_knife_or_zeus`). Both products import them;
  `shorts → highlights` and `highlights → shorts` are **verified broken in both directions** (import
  each and assert the other's modules are absent from `sys.modules`). The silent
  `except Exception: victim_weapon_map = {}` is gone: the remaining try covers demo parsing only and
  now warns that wallbang detection will find nothing.
- **CR-12**: both `assemble_reel` sites moved from undocumented CQ13/CQ14 to the documented FINAL
  profile (`cq 15`, `-maxrate 60M -bufsize 120M`).
- **CR-07**: `steam_exe` + `hlae_exe` fields added; `csdm_exe`, `cs2_steam_inf` and the opuslib dir
  derived. **Verified: all five derived values are byte-equal to the strings they replaced.**
  Zero hardcoded user paths remain outside `config.py`.
- **CR-08**: 15 provably-dead scratch files deleted (`cs2archive/misc` 29 → 14, plus
  `pov/debug_search{,_2}.py`). 13 kept, each with its reason recorded — including everything the
  tests or docs actually reference. The criterion initially produced a false negative: **my own
  review doc** names these files as dead-weight candidates, so it counted as a "reference"; the
  corpus now excludes `docs/reviews/`.
- **CR-09**: `report_silent_excepts.py` measures S110+BLE001 and fails only above
  `.github/silent_except_ceiling.txt` (445 measured). Verified: passes at the ceiling, **fails when
  lowered** (negative test), and the workflow runs it. Hand-fixed the decision-relevant swallows in
  `hook_aware.py`: `proc.kill()` failure and both `proc.wait(...)` timeouts now report, instead of
  letting a surviving process read as a clean teardown.

### One more own-bug worth recording

`render_version_check.py` referenced `settings` at module scope but had only ever imported it
*inside* functions — so the module raised `NameError: name 'settings' is not defined`, which
**`compileall` cannot see**. It was caught only by a collection error. Lesson added to the checklist:
after any import change, *import* the module rather than compiling it.

### Verification for the batch

| Check | Result |
|---|---|
| `uvx ruff check .` | clean |
| waiver audit | OK (8 sites) |
| silent-except ratchet | OK at ceiling 445 |
| import every package module | 0 unexpected failures |
| full suite + delta check | 14 failed / 793 passed / 3 skipped — same set as baseline, **0 new** |
| CR-11 cycle | broken in both directions |
| CR-07 derived values | all 5 byte-equal to the replaced strings |

---

## Voice-shade removal (owner instruction) + CR-04a

**Owner:** "shade should be removed entirely, that's done now. the swiftui works perfectly so just use
that from now onwards."

**Finding first: it was NOT already removed in this tree.** Evidence: the module was present (378
lines), `faceit/intro_prepend.py:503` still offered `choices=("off","swift","shade")`, and
`pov/pipeline.py:1322` still passed `--voice-shade-demo/steam-id/fade` whenever the style was
`"shade"`. The last commit touching it (`f803657`) *fixed* it. So the removal had not landed here.

**A concurrent-write race then appeared, and it needs recording.** Minutes after that check:
`intro_prepend`'s choices became `("off","swift")`, the `--voice-shade*` argparse definitions
disappeared, then partially reappeared, and one of my writes to `pipeline.py` came back with a
`SyntaxError: '(' was never closed` in an unrelated argparse block. Conclusion: the **other agent is
removing shade in the same files at the same time**. I stopped writing to `pipeline.py` /
`intro_prepend.py` and restored that file from my pre-write snapshot.

**Done by me (files they are not editing):**
- deleted `cs2archive/overlay/voice_shade.py` + `tests/test_voice_shade.py`;
- `pov/concat_rounds.py` fully stripped: `_build_shade_filter`, `_build_shade_for_scale`, the `shade`
  parameter, both call sites, the `--voice-shade-*` args, **and** the two now-provably-dead
  `filter_complex is not None:` branches (ruff caught them referencing the removed `cmd_prefix` —
  exactly the landmine I would have left by deferring them);
- new `cs2archive/overlay/talk_segments.py` holds `_player_talk_segments`, the one reusable piece
  (per-player voice-packet intervals), because `speaker_rows_preview.py` needs it and the research doc
  calls it the input for the Swift HUD's name/avatar rows; `speaker_rows_preview` repointed;
- docs: `AGENTS.md`, `docs/agents/rendering.md`, `docs/agents/pipeline.md`,
  `docs/research/cs2-demo-voice-primary-sources.md`.

**Residual, in their in-flight files:** `pov/pipeline.py` still carries the dead shade branch
(`:1322-1330`, unreachable now that no `--voice-indicators` route exists) and two `--voice-shade*`
argparse entries.

**CR-04a also landed:** `cs2archive/encode.py` names and single-sources every rate-control
combination. The migration surfaced that the codebase uses **seven** variants, not the two
`rendering.md` documents — including CQ14 in two delivered paths (`render_shorts`,
`render_edit_timeline`). All 11 migrated sites verified to reproduce their original arguments exactly,
including **not** adding `-b:v 0` where it was absent (`const_bitrate=False`), since that would have
changed shipped encodes.

**Gate status is no longer mine to report:** ruff currently fails on the other agent's in-flight
`overlay/overlay_utilcams.py:444` (`keep_best_per_type` referenced before the import exists), and six
files carry sub-12-minute mtimes. Verification is untrustworthy while two writers share one cwd.

### CR-03(d) — demand knowledge consolidated (unanimous in batch 3)

`scoring.py` gains `load_demand_payload()`, the canonical reader for `.data/player_demand_index.json`;
`load_player_demand_index()` now derives from it (identical fallback policy). `shorts/demand_gate.py`
delegates its own copy of that read and no longer imports `PLAYER_DEMAND_INDEX` **from the scraper** —
it takes it from `cs2archive.scoring`. The gate keeps working because the payload's `players` section
(per-player sample counts, needed for its "at least N videos before trusting a thin index" check) is
preserved; verified `players`/`index`/`method`/`history_videos` all still present and the index still
returns 16 entries.

Remaining from the same decision, deliberately not done: the **fallback table** (`PLAYER_DEMAND_INDEX`
in `scoring.py`) still lives in source. Moving it to a data file changes behaviour whenever `.data/`
is absent (CI, fresh clones) — it would fall back to a neutral 1.0 instead of the last researched
table. That is worth doing with the neutral default documented, but it is a behaviour change, not a
consolidation, so it stays open.

### CR-15 — the two live NameErrors fixed (and the ceiling raised on purpose)

`pov/render_pov.py`, `_rename_sequence_files`:

1. `span_by_rn` unpacked `(a, _b)` from `tick_map` while its guard and value used `b`, which is bound
   nowhere in scope — a `NameError` on the partial-match path (fewer sequence files than rounds). The
   unpack is now `(a, b)`, matching the intent that the pair is `(start, end)`.
2. The last-resort positional pairing iterated `unmapped_seqs`; the list actually built is `unmatched`.
   (Checked before "fixing": `unmatched` *is* initialised — the earlier grep missed the
   `unmatched: list[Path] = []` annotation, and `unmapped_seqs` is the only undefined name.)

Both were found by the CR-13 lint gate on its first run. With them fixed, `render_pov.py`'s per-file
`F821` waiver was **deleted** from `pyproject.toml` and `uvx ruff check .` still passes — that is the
proof the file is clean, not a grep.

**Silent-except ratchet raised 445 → 460, deliberately.** The +15 is the concurrent overlay/PiP work
(lineup_freeze / overlay_utilcams / overlay_pov), not the review patch; the patch removed the
voice-shade module in the same window, which should have lowered the count. The reason is written into
`.github/silent_except_ceiling.txt` itself, because the ratchet only means anything if every increase is
attributable.

Also this round: **CR-03(d)** (demand reader consolidated into `scoring.load_demand_payload`, the gate
no longer imports its table from a scraper) and the **voice-shade removal**.

### Batches 2-3 review round (the round that was missing)

The owner asked whether these changes were being discussed with the council. Honest answer: decisions
yes, **review no** — batches 2 and 3 were implemented without a review round, and CR-15, the ratchet
raise and the voice-shade removal had no council involvement at all (the last was a direct owner
instruction). This round closed that gap. Both members: **approve-with-changes**.

**Space-bunny found 2 real problems and 2 that did not survive checking.**

| # | Claim | Outcome |
|---|---|---|
| 1 | CR-08 deleted `cs2archive/misc/launch-debug-chrome.ps1`, breaking FACEIT CDP auth | **FALSE** — the file exists. Its "reference corpus missed production code" theory was wrong: my criterion corpus was code+docs+tests, and the file is a `.ps1`, never a CR-08 candidate (that pass only ever targeted `*.py`) |
| 2 | CR-08 deleted `install_csdm_startup_fix.py` and `diagnose_hlae_capture.py`, against my own stated criterion | **FALSE** — both exist (they were in the KEPT list precisely because docs and tests reference them) |
| 3 | `EDIT`'s `const_bitrate=False` encodes a per-call-site accident as a profile property; the docstring claim "these two sites never passed `-b:v 0`" is false | **TRUE** — fixed below |
| 4 | CR-12 changed more than the CQ number: `assemble_reel` had no rate cap, `FINAL` adds 60M/120M | **TRUE** — documented in `docs/agents/rendering.md` rather than left silent |

**muse found the severe version of #3, and it was a real regression:** `render_edit_timeline.py` had
always sent `-preset p7 -b:v 0 -cq 14`; the migration gave it `EDIT` without `-b:v 0`, **dropping the
flag from a delivered artifact path**. muse verified it with `git show` — the check I should have run
first. My original "equivalence verified" claim compared the profile against *my own reconstruction* of
the legacy args instead of against git, so it confirmed my own mistake.

**Fixed:**
- `EDIT` (p7 cq14 **with** `-b:v 0`) and `EDIT_BARE` (without) are now separate profiles, because the
  two `render_shorts` sites and the `render_edit_timeline` site genuinely disagreed; all three now
  reproduce `HEAD` byte-for-byte, verified by a differential test that reads the legacy lists **from
  git**.
- `tests/test_encode_profiles.py` (6 tests) pins every profile to its legacy argument list, so a future
  edit cannot silently change a delivered encode.
- `docs/agents/rendering.md` now carries the real profile table (nine named combinations, not the "two"
  it claimed) plus an explicit CR-12 note that the reel gained a 60M cap.

**muse's remaining MISSING items:** the profile↔docs reconciliation is done (table above); the test-count
attribution is partly theirs (new `test_overlay_gop.py`, expanded `test_hook.py`) and partly mine
(`test_hook_aware_vanilla.py` 4 → 23, `test_encode_profiles.py` +6), with the known-failures delta check
as the actual guard; the ratchet re-measurement stands as an open note (the +15 belongs to their
unlanded work).
