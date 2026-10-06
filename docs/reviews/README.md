# Code reviews

Recurring, whole-repo maintainability audits of CS2Archive. Each review is a dated
snapshot: it records what the codebase looked like at a commit, what was wrong, and
what to change. Reviews are **not** updated after the fact — a later review supersedes
an earlier one, and the register below is what carries status forward.

## Layout

```
docs/reviews/
  README.md                              ← this file: convention + open-item register
  YYYY-MM-DD-<slug>-code-review.md       ← one report per review
```

Slug names the review type, e.g. `thermo-nuclear`, `security`, `performance`.

## How to run one

1. Pin the commit under review (`git rev-parse HEAD`) — the report must name it.
2. Run the review with the Cursor **thermo-nuclear code quality review** skill
   (`cursor/plugins` → `cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md`).
   For a whole-repo pass (not a diff), additionally sweep:
   - file sizes + per-function length/AST metrics
   - `sys.path` manipulation and duplicate module basenames
   - copy-pasted constants and near-duplicate helpers
   - `except Exception` / silent-fallback density
   - typed-boundary density (`@dataclass` count vs `: dict` parameter count)
3. Long files can be delegated to read-only sub-agents in parallel — one per subsystem.
   **Verify every delegated claim against the file before publishing it.** One delegate in
   the 2026-09-27 review fabricated a function that does not exist (see that report's
   "Method and confidence" section).
4. Write the report using the template below, then update the register.

## Report template

```markdown
# <Title>
- **Date:** YYYY-MM-DD
- **Commit reviewed:** <sha> (<subject>)
- **Scope:** whole repo | subsystem | diff vs <ref>
- **Method:** ...
- **Verdict:** approvable | not approvable as-is

## Verdict / blockers
## Findings            (CR-nn, each with severity + file:line evidence)
## Code-judo moves     (ranked, each with what it deletes)
## Change batches      (A mechanical → D policy, checkboxed)
## Guardrails          (invariants that must not regress)
## Method and confidence  (verification legend, delegate reliability)
## Superseded/open items
```

Each finding gets a stable id (`CR-01`…). Ids are referenced by the register and by
follow-up work, so **do not renumber** — mark a finding `superseded by CR-nn` instead.

## Verification legend

Findings and leads are tagged so a reader knows how much to trust them:

| Tag | Meaning |
|---|---|
| `[verified]` | Re-checked directly against the source by the reviewing agent |
| `[lead]` | Reported by a delegated sub-agent, not independently re-checked |
| `[refuted]` | Claimed during the review and proven false — retained so it is not re-raised |

## Open-item register

Statuses: `open` · `in progress` · `done` · `deferred` · `superseded`.

### 2026-09-27 — thermo-nuclear code review

Report: `2026-09-27-thermo-nuclear-code-review.md` · commit `eba2f87`

| Id | Finding | Severity | Status |
|---|---|---|---|
| CR-01 | `sys.path` bootstrap / no package: dual import identity for `scripts` | blocker | **done** (2026-09-27; see council log — patch reviewed by both council members, 7 defects found and fixed) |
| CR-02 | Ten files >1000 lines; `detect_shorts()` is one 832-line function | blocker | open |
| CR-03 | Domain logic duplicated across products (round/winner/tail/weapon rules) | blocker | open |
| CR-04 | Encoder, subprocess, JSON and retry helpers re-implemented per file | major | open |
| CR-05 | Untyped boundaries: 8 dataclasses vs 251 `: dict` params | major | open |
| CR-06 | Pipeline double-implements its steps; 10 ad-hoc resume checks, 5 size thresholds | major | open |
| CR-07 | Canonical `settings` paths bypassed by 10 modules | minor | **done** (2026-09-27) — 5 paths routed through `settings` (2 new fields `steam_exe`/`hlae_exe`; 3 derived `csdm_exe`/`cs2_steam_inf`/opuslib), derived values verified equal to the old hardcoded strings. Only the overridable `config.py` defaults remain, by agreement |
| CR-08 | Dead weight: tracked `.deprecated`, one-off scripts, dead code | minor | **partly done** — `build_kill_timeline.py.deprecated` and the `_minimize_cs2` importlib workaround removed by CR-01; the rest open |
| CR-09 | Silent-failure culture: 371 `except Exception`, 91 silent `pass` | major | **partly done** — S110/BLE001 ratchet in CI (ceiling 445, may only shrink); kill/verify/render-decision swallows fixed in `hook_aware.py` (`proc.kill`, both `proc.wait` sites). The remaining 440-odd sites are open |
| CR-10 | HLAE process manager treats "measurement failed" as "process absent" | major | **done** (2026-09-27) — three-state `ProcessQuery`, unknown never read as absent, bounded retries, distinct abort reason, `_taskkill_pid` now returns a status. 16 tests. See the council log |
| CR-18 | `kill_stale_processes()` and `_clear_all_cs2()` kill by image name/global query with **no ownership check**, so two concurrent renders can kill each other's cs2/HLAE/ffmpeg (found by space-bunny during CR-10 review) | major | open — needs per-render pid ownership (pipeline scope). The CR-10 narrow guard only refuses blind kills when the query is UNKNOWN; it does not fix the known-state collision |
| CR-11 | Cross-product circular import with a silent fallback that wipes wallbang detection | major | **done** (2026-09-27) — `cs2archive/weapons.py` holds both vocabularies; the cross-product cycle is broken in both directions (verified) and the silent `victim_weapon_map = {}` wipe now warns |
| CR-12 | Live encoder drift: `assemble_reel` encodes CQ13/CQ14, not a documented profile | major | **done** (2026-09-27) — both `assemble_reel` sites now use the documented FINAL profile (`cq 15`, `-maxrate 60M -bufsize 120M`); CQ13/14 were undocumented drift |
| CR-13 | No lint gate, no CI, no `pyproject.toml` | major | **done** (2026-09-27) — `pyproject.toml`, ruff gate (`F`+`E9`, pinned 0.16.9), waiver audit, known-failures delta check, GitHub Actions workflow. See the CR-13 section of the council log |
| CR-14 | `_map_mesh_available()` inserted only the sibling repo root, so `render.map_collision` never imported and the documented `insta_kill` mesh guard was permanently tripped | major | **done** — found by space-bunny during the CR-01 review, fixed by calling `prefer_cs2util_scripts()`; verified mirage/nuke/dust2 now load collision grids |
| CR-15 | Lint debt deferred by the CR-13 waivers: 216 `F401`, 51 `F541`, 20 `F841` — **plus two live NameErrors the gate found on its first run** (`pov/render_pov.py:268-270` a comprehension that unpacks `(a, _b)` but guards on `b`, and `:311` an undefined `unmapped_seqs`), and 3 `F811` / 1 `F402` in overlay files | major | **partly done** (2026-09-27) — the two live `NameError`s in `pov/render_pov.py` are fixed: the `span_by_rn` comprehension unpacked `(a, _b)` while its guard tested the never-bound `b`, and the positional-pairing fallback referenced `unmapped_seqs` instead of `unmatched`. The file's per-file `F821` waiver is **removed** (gate covers it again, verified). Open: the 460-site S110/BLE001 debt (ratcheted in CI), 3x F811 + 1x F402 in overlay files, and the F401/F541/F841 waivers |
| CR-16 | Suite determinism: ~~two intermittent `test_swift_demoui.py` tests~~ **fixed** — they depended on real ambient processes, so each now pins `_live_render_processes` explicitly (stable across 5 runs: 3 file-level + 2 full-suite, 14 failed / 778 passed, identical sets). Remaining: `tests/test_overlay_extraction.py` is a CLI script whose `test_*` helpers are mis-collected as tests | minor | **mostly done** — collection suppressed via `tests/conftest.py` (pytest 9.1.1 rejects the ini form). Open: decide the helper rename (`test_vs_ground_truth`/`test_velocity_yaw` → `check_*` + the two call sites at lines 223-224). Expires 2026-11-15 |
| CR-17 | Five baselined failures in `tests/test_build_short_timeline.py` (`test_clutch_detected`, `test_clutch_detected_when_30s_disadvantage`, `test_2v4_2v5_counted`, `test_zero_kill_defuse_clutch`, `test_mixed_timeline_coexists`) are one root cause, not five: the detector returns zero shorts where the fixtures expect a clutch/4K | major | open — grouped by the CR-13 council review so the baseline does not read as five unrelated silences. Same family as CR-02/CR-03 |

Batch checklists live at the end of each report.

### 2026-09-28 — timeline v3 (stakes + zones + pro ledger) review

Report: `2026-09-28-timeline-v3-review.md` · commit: none (`cs2archive/` untracked)

| Id | Finding | Severity | Status |
|---|---|---|---|
| CR-19 | Bomb moment `zone.site` (and `bomb_actions[].site`) is a C4 entity handle (`build_action_timeline.py:1382`, `:696`, `:715-716`) — new v3 field ships garbage; also leaked into the LLM prompt (`build_edit_timeline.py:268`) | major | open — fix is the planter's `last_place_name` at the action tick (verified 10/10 plants) |
| CR-20 | First round dropped on every HLTV/PBDEMS2 demo probed (`round_start` for round 1 is at tick 1; `:1053` filter) → 19 rounds, `score_after` 12-7 for a 13-7 match, `arc.pistol_rounds == [13]`, round 1 missing from `kills`/`moments`/`pros`/`stakes`. Pre-existing, FACEIT-unaffected, but the v3 "R1 pistol" evidence does not generalise | major | open |
| CR-21 | `_derive_pro_ledger` accepts and ignores `round_deaths` (`:887`); `pros[].deaths` counted from `kills_all`, which excludes teamkills/suicides/world deaths — disagrees with `rounds[].alive` | major | open |
| CR-22 | Unresolved places are silent (no `place_source` guard, unlike `buy_source`), and `_place_at`'s `tick - 2` probe can never hit (`:1110-1112` vs `:1145`) | major | open |
| CR-23 | `STAKES_VERSION` is written but read by nothing (its `INSTA_RULE_VERSION` comment is false); `multipov/edit_timeline.json` records no source version and its gate is `< 2` | major | open |
| CR-24 | Docstrings assert false facts: `balance` is not flat-16000 at freeze ticks (refuted); `site` is not a bombsite (`:23`, `:36-40`, `:850-854`) | minor | open |
| CR-25 | `stakes.equip_value` drops `players`, so the per-player average it was classified on is not recoverable; `buy_source` is per-round, not per-team | minor | open |
| CR-26 | Decisions 1-7 verdicts: keep 1,3,4,5,6,7 (with riders) and keep 2's omission while overturning its reason; approved value-function change is buys-as-veto-plus-promotion, **not** a literal `tier_ok` replacement (would resurrect the SIEZ Tec-9 4K skip) | — | see report §1/§5 |

Test gaps before scoring integration (report §3): `_economy_by_round` untested; the bomb-zone test
encodes a value the producer never emits; no "v2/unknown buys change nothing" regression test; no pro
deaths from teamkill/suicide/world; no version-pinning assertions.

### 2026-10-05 — pipeline performance TODO list

Backlog: [`2026-10-05-pipeline-performance-todo.md`](2026-10-05-pipeline-performance-todo.md) · commit `8453f1c`

All items **open**; no implementation or controlled A/B benchmarks performed. Savings and council claims are corrected/qualified in the backlog. Encoder quality and product selection remain unchanged.

| Id | TODO | Status |
|---|---|---|
| PERF-01 | Benchmark native concat + upscale inside final overlay encode; preserve geometry/resume/raw-only | open |
| PERF-02 | Measure concat/audit/remux/copy separately; optimise without weakening validation | open |
| PERF-03 | Benchmark duration-aware batching and interrupted-run recovery; choose policy | open |
| PERF-04 | Shared action-timeline/stat-strip caches outside purgeable POV folders | open |
| PERF-05 | Profile and deduplicate hook/event parsing with versioned equivalent outputs | open |
| PERF-06 | Memoise voice eligibility/team metadata | open |
| PERF-07 | Parse team roster once per demo during backlog creation | open |
| PERF-08 | Cache prosettings; reuse embedded crosshair code from valid analysis | open |
| PERF-09 | Reduce output copying and repeated assembly rewrites safely | open |
| PERF-10 | Reuse unchanged hook render windows | open |
| PERF-11 | Hoist keyboard-mapper invariant allocations | open |
| PERF-12 | Measure listener discovery latency/idle scraping; preserve single renderer | open |
| PERF-13 | Diagnose listener restart warnings from correlated evidence | open |
| PERF-14 | Owner decisions: PiP volume and any separate mezzanine quality experiment | open |

## Other reviews in this folder

| Report | Scope |
|---|---|
| `2026-09-27-thermo-nuclear-code-review.md` | Whole-repo maintainability review (CR-01…CR-18). Has a **RESUME HERE** status block at the top. |
| `2026-09-28-timeline-v3-review.md` | Timeline v3 (`detect_shorts` / edit-timeline) review from the overlay/PiP session. |
| `2026-10-05-pipeline-performance-todo.md` | Pipeline performance TODOs, benchmark/parity plan, council corrections and deferred proposals (PERF-01…PERF-14). |
