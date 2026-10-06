# Pipeline performance review — TODO list

- **Date:** 2026-10-05
- **Commit reviewed:** `8453f1c5a8dfb138bdeec96d9c6c12993152f2a0`
- **Scope:** CS2Archive production POV flow, with shared CS2UtilArchive ingestion/render paths.
- **Method:** Three Herdr advisors, same brief, three discussion passes; parent source checks and existing production logs. No implementation or controlled A/B benchmark performed.
- **Status:** All actionable items below remain open. This is a work backlog, not permission to weaken quality/correctness guards.

## Guardrails

Keep delivered resolution, aspect stretch, encoder settings, audio normalisation, PiP/keyboard appearance, detection rules and selection policy unchanged. Preserve raw-only output, resume, freeze restore, sidecar sync and process ownership. Never run concurrent CS2/HLAE renders. Do not delete demos or avatar caches. Do not treat advisor agreement as measured performance evidence.

## Baseline to reproduce

Existing log: `logs/2398725_spirit-vs-parivision-m3-anubis_donk_Anubis.log`.
18 rounds, approximately 1766 seconds of video, capture 1440×1080, delivered 2560×1440@60.

| Phase | Historical observation |
|---|---|
| POV render | 1571 s |
| Native concatenated video | 16192 MB as reported by log |
| Separate upscale + CQ8 encode | 1436 s; output 41569 MB |
| Overlay total | 3398 s |
| Utility flight render | 1365 s; 42 clips, 41 PiPs, one batched launch |
| Overlay batch encodes | 1443.5 s total |
| Batch concat + validation | 416.5 s aggregate; component costs NOT measured separately |
| Keyboard tick extraction | 14 s reported; CPU work overlapped flight rendering |

These are one run's observations, not general guarantees. Log sizes use its reported MB convention. Earlier council claims overstated savings: removing a 1436 s pass does not guarantee 1436 s net saving. Replacing a 41569 MB intermediate with 16192 MB saves roughly 50.8k reported MB across one write and one read, not 83 GB; actual traffic depends on retained files and the new graph.

## Priority 1 — benchmark before architectural changes

### PERF-01 — Fuse base upscale with final overlay encode [verified structure; benefit unproven]

- [ ] Prototype behind an opt-in flag: concatenate/transition at capture-native resolution; scale base to export resolution inside the overlay filtergraph **before** placing keyboard/PiPs; encode final video once.
- [ ] Keep separate upscale for raw-only delivery and manual workflows that require exported combined video.
- [ ] Thread explicit export width/height through overlay, sprite generation, PiP geometry and flight capture sizing. Do not infer export geometry from native input dimensions.
- [ ] Audit `pipeline.py` auto-skip/resume gates, `concat_rounds.py` resolution-based resume, batch stamps, freeze restore, sidecars and hook/outro/thumbnail consumers.
- [ ] Store sufficient provenance: capture dimensions, intended export dimensions, transform/source stamp. Make flag changes idempotent; never silently downscale an existing upscaled mezzanine and then upscale it again.
- [ ] Preserve native-resolution round-boundary dissolves and their adjusted tick/frame offsets.
- [ ] Benchmark actual existing vs proposed overlay graphs on the same source, including GPU decode/scale, hardware download/upload, CPU compositing and NVENC contention. Standalone scaler benchmarks are insufficient.
- [ ] Measure total step-3 + step-4 elapsed time, intermediate size, read/write traffic, peak disk use, GPU/CPU utilisation and memory. Net saving = old upscale + old overlay − new combined overlay, with other changed work included consistently.
- [ ] Compare final dimensions/SAR/fps, frame counts, durations, audio sync and loudness, GOP gaps and monotonic PTS. Inspect decoded frames at PiP centre/edges, keyboard idle/press, round dissolve, hook/intro joins. Use SSIM/PSNR as supporting evidence, not sole proof of visual parity.
- [ ] Regression coverage: native 4:3 and 16:9, export size overrides, raw-only, keyboard off, freeze on/off, stale pre-existing scaled output, interruption/resume and changed flags.
- [ ] Start with a short two-round fixture, then one representative full POV; record commands, versions, timings and output paths before enabling by default.

Primary files: `cs2archive/pov/concat_rounds.py`, `cs2archive/pov/pipeline.py`, `cs2archive/overlay/overlay_pov.py`, `overlay_encode.py`, `overlay_utilcams.py`, `render_util_cams.py`.

### PERF-02 — Attribute concat, audit and remux costs [benchmark required]

- [ ] Time stream-copy batch concat, GOP/PTS validation, audio remux and destination copying separately. The 416.5 s historical aggregate does not prove GOP audit dominates.
- [ ] Inspect `_audit_gop`/ffprobe implementation and distinguish packet scans, frame decoding and disk-bound work; measure each rather than assuming.
- [ ] For one batch, investigate direct promotion/rename or hardlink instead of rewriting video. Preserve audio handling and source lifetime semantics.
- [ ] Keep correctness validation for single batches: lack of a join rules out join defects, NOT internal GOP holes, malformed timestamps or corruption.
- [ ] If avoiding repeated audits, require trustworthy per-batch validation stamps bound to source/params, then validate joined boundaries and global timeline properties. Do not replace full coverage with boundary samples without proving equivalent protection.
- [ ] Add fixtures for one corrupt batch, mixed copied/re-encoded batches, internal keyframe gaps, boundary PTS regressions and stale stamps.

Primary files: `cs2archive/overlay/overlay_encode.py`, `overlay_pov.py`.

### PERF-03 — Choose duration-aware batch policy [owner decision]

- [ ] Clarify current option semantics: pipeline passes **rounds per batch**, not necessarily a batch count. Keep explicit CLI overrides compatible.
- [ ] Remove/default-align the direct overlay vs pipeline discrepancy (5 vs 10 rounds per batch during review).
- [ ] Benchmark one batch vs current round batches; compare elapsed time, memory, graph/input limits and recovery work after a deliberate interrupted test on disposable outputs.
- [ ] Choose duration-based boundaries or target encode-time units from evidence, not an arbitrary round threshold.
- [ ] Record policy decision and expected failure/recovery tradeoff. Retain practical resume units for long POVs.

Council threshold remained split: ≤40–60 minutes for one batch versus ~10-minute units. **Correction:** `ceil(1766/600)` gives three batches, so the council did NOT unanimously agree this POV should use one batch.

## Priority 2 — narrow caching and I/O improvements

### PERF-04 — Reuse per-demo action timeline outside purgeable POV folders

- [ ] Check existing `paths.find_action_timeline`/backlog-created caches before rebuilding.
- [ ] Give shared demo-derived artifacts a canonical home not removed by per-POV post-upload cleanup; do not repurpose `renders/hl-*` contrary to product layout without an explicit decision.
- [ ] Key by canonical demo identity/source fingerprint and schema/rule version, not stem alone; use atomic writes and safe concurrent readers.
- [ ] Preserve POV-local compatibility copies/links where required and test second-POV cache hits plus source/version invalidation.
- [ ] Apply same lifetime analysis to `stat-strips/` sibling reuse: establish whether upload cleanup destroys the only reusable copy.

Measured action-timeline build was ~10 s on one small FACEIT demo; savings scale with actual repeated POVs, not backlog card count.

### PERF-05 — Avoid repeated detector/event parsing

- [ ] Profile hook short/action/rewind builders individually on representative HLTV and FACEIT demos. Historical advisor measurement: ~33 s total on a 242 MB FACEIT demo; not a universal 3–8-minute tax.
- [ ] Consider a process-local memoised parser/event facade before introducing a large shared tick cache.
- [ ] Cache rewind candidates only if output equivalence is demonstrated; retain all-player trade context and POV-dependent dense snapshots. Do not assume unfiltered detection costs the same as one-player detection.
- [ ] Separate detection-cache versions from selection-only hook parameters so selection tweaks need not repeat valid detection.
- [ ] Reuse demo-level round events while preserving tick-0/knife-round/POV-death rules.
- [ ] Verify parse call counts and output equality on fixed demos. Account for work already hidden behind flight rendering when calculating net elapsed savings.

Primary files: `pov/build_hook_timeline.py`, `overlay/victim_rewind.py`, `highlights/build_action_timeline.py`, `shorts/build_short_timeline.py`, `overlay/overlay_pov.py`.

### PERF-06 — Memoise voice eligibility per pipeline instance

- [ ] Cache `_voice_enabled()` after demo/steam ID resolution, following `_cached_rename_map` pattern.
- [ ] Check repeated `load_team_map`/voice loading across eligibility checks and voice-mix subprocesses; reuse safe demo-derived metadata where useful.
- [ ] Test call counts, disabled voice, missing voice and different POV teams. Do not cache across changing inputs.

Historical advisor timing: `load_team_map` ~2.24 s per call, four repeated checks (~9 s).

### PERF-07 — Parse team roster once per demo during backlog creation

- [ ] Replace per-card `detect_pov_opponent` full tick parsing with a per-demo roster lookup, using/validating existing `org_per_player` logic.
- [ ] Include the shorts metadata pass in reuse; its per-Steam-ID cache still repeats work across players.
- [ ] Test opponent derivation, side swaps, aliases, split demos and failed detection fallback. Match existing authoritative demo-vs-HLTV behaviour.
- [ ] Measure backlog creation with multiple player/map cards and assert one relevant parse per demo rather than one per card.

Primary files: `pov/create_backlog.py`, `shorts/detect_team.py`, `shorts/team_roster.py`. Field set is small; absolute savings remain unmeasured.

### PERF-08 — Cache prosettings and pass through embedded crosshair code

- [ ] Add bounded positive/negative TTL caching for identical live prosettings lookups across POV/hook/intro processes; distinguish absent profile from transient network failure.
- [ ] Preserve freshness policy and prevent one temporary error becoming a long-lived fallback-quality downgrade.
- [ ] Use existing `csdm_analysis.json` share code when valid instead of exporting the whole demo again; retain fallback for standalone commands, missing/stale analysis and split demos.
- [ ] Test network hit/miss/error, TTL expiry, known pro/unknown nick, invalid share code, source fingerprint and capture-height scaling.
- [ ] Count actual network requests and `csdm json` invocations on a normal successful lookup and on an absent profile; benchmark the latter.

`resolve_crosshair` returns before the lazy demo-export fallback when prosettings yields cvars. Re-export is conditional, not unavoidable on every POV. Normal repeated GET cost is small; three 60 s retries plus sleeps create an outage-shaped worst case. Some FACEIT players may routinely use the fallback; measure prevalence rather than assuming.

### PERF-09 — Reduce deliverable copying and repeated assembly rewrites

- [ ] Benchmark overlay→YouTube copy separately; consider same-volume hardlink with copy fallback only after proving every later writer replaces files rather than modifies shared inodes in place.
- [ ] Verify cleanup/unlink behaviour, external consumers, cross-volume failure and resume.
- [ ] Consider one final hook + intro + POV + outro assembly instead of repeated full-file rewrites, after measuring actual cost.
- [ ] Preserve AAC 48 kHz stereo normalisation, mixed-encoder audio decode/join, fades, sidecar offsets, idempotent prepend markers and video stream-copy compatibility.
- [ ] Compare video packet identity where appropriate plus decoded audio/sync and interrupted assembly recovery.

Primary files: `pov/pipeline.py`, `faceit/intro_prepend.py`, `pov/assemble_hook.py`. Not a zero-risk filesystem change.

### PERF-10 — Reuse unchanged hook render windows

- [ ] Key clips by demo fingerprint, tick window, capture/encoder params and complete cfg/avatar policy, not sequence number alone.
- [ ] Preserve valid unchanged clips when selection/max-duration params change; render only added/changed windows.
- [ ] Test changed ordering/count/windows, changed crosshair/HUD/rule versions and stale clip rejection.

Primary file: `pov/render_hook.py`. Benefit mainly on retuning/recovery, not every first run.

### PERF-11 — Hoist keyboard-mapper invariant allocations

- [ ] Hoist per-frame round-offset list construction and per-row invariant default-state construction in `overlay_pov.py`.
- [ ] Check whether redundant sorting can safely be skipped; do not assume input order without proof.
- [ ] Benchmark mapper alone and assert exact per-signal/frame equality, including boundaries and missing ticks.

Small CPU improvement; current CPU extraction already overlaps util flight rendering.

## Priority 3 — operations and policy, not steady-state encode speed

### PERF-12 — Listener discovery responsiveness

- [ ] Record poll latency while a pipeline runs; distinguish responsiveness from saved render time.
- [ ] If fresher queue ranking is valuable, decouple discovery from the render worker while enforcing exactly one renderer/process owner; do not launch concurrent pipelines.
- [ ] Measure idle HLTV browser launches and demand refresh calls; evaluate persistent-session reuse, TTLs and idle backoff without weakening freshness gates.

Synchronous queue execution is real; two ~114-minute renders imply ~16% daily busy time under a two/day cap. That estimate alone does not prove missed cards or zero operational impact. This is not a measured POV speed saving.

### PERF-13 — Investigate listener restart warnings separately

- [ ] Correlate each wrapper exit/restart timestamp with actual daemon stderr, return code, state updates and browser errors.
- [ ] Check whether repeated failures are currently ongoing or historical, and whether the wrapper's restart/rebaseline behaviour loses/retries queue entries.
- [ ] Diagnose before changing retry policy; distinguish browser startup failure, lock contention, pipeline hard failure and intentional termination.

Observed: repeated `Listener exited with code 1; restarting in 30 seconds.`; a state snapshot had empty queue and `BrowserType.launch_persistent_context: Target page, context or browser has been closed`. Neither snapshot proves the restart cause. The council's specific failing-card crash-loop theory was **not established**, and the browser attribution also remains a lead.

### PERF-14 — Product decisions: PiP volume and mezzanine profile

- [ ] Review PiP count only if requested: historical 41 PiPs cost ~23 minutes. Reducing count/filtering changes product behaviour; do not silently cap it.
- [ ] Leave encoder quality settings alone for this work. Revisit mezzanine settings only via a separate quality experiment if pass fusion does not deliver enough benefit.

## Deferred/rejected proposals and corrected claims

- **Overlay keyboard + PiP double encode:** not default-path waste; batched path merges them into one encode. Explicit `--overlay-batches 0` uses a fallback path; missing authoritative sidecar data is refused. Freeze is opt-in and not default cost.
- **Merge hook and POV capture:** technically audio/HUD fields are per-sequence (`csdm_segments.sequence`), contrary to one advisor's batch-global-audio rationale. Defer for small estimated boot saving, planning order, failure coupling and resume blast radius. Do not assume every hook failure currently soft-skips; inspect actual pipeline error handling.
- **Pre-render all players' util flights in one demo launch:** clips are thrower-specific; this saves boots, not per-clip recording. Defer unless actual same-demo multi-POV throughput justifies extra rendering, cache lifetime and partial-finalise complexity.
- **Skip GOP audit because one batch is automatically valid:** rejected reasoning. One batch can still be invalid internally; optimise validation only with equivalent coverage.
- **Audit consumes the entire 416.5 s:** unverified; benchmark component timing first.
- **Guaranteed ~31-minute combined saving:** unproven. Scale cost moves into overlay, audit/copy costs overlap and batching tradeoffs remain. Report measured end-to-end A/B results instead of adding estimates.

## Benchmark deliverable checklist

- [ ] Pin code commit, Python/CSDM/HLAE/CS2/ffmpeg versions, hardware, demo identity, player and exact flags.
- [ ] Use identical source artifacts and selection; distinguish warm/cold caches and startup from steady-state work.
- [ ] Record commands, per-phase monotonic elapsed times, output sizes, peak storage/memory and CPU/GPU/IO utilisation.
- [ ] Repeat short benchmarks to expose variance; finish with a representative full-POV A/B where safe.
- [ ] Save visual/audio/PTS/GOP parity evidence and regression-test outcomes.
- [ ] Publish actual net savings and any recovery tradeoff before changing production defaults.

Raw council transcripts/briefs were captured under `.scratch/` (`full-*`, `p2-*`, `p3-*`, `council-pass*-challenge.md`). Those are ephemeral supporting artifacts, not authoritative proof or durable dependencies of this TODO list. Stable statuses live in `docs/reviews/README.md`.
