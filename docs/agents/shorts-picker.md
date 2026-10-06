# Shorts view prediction and daily picker

**Live-validation finding (2026-10-06):** the picker has a reproduced
single-wallbang/compound-highlight feature mismatch and first-round extraction
loss. The aggregate-loss edge below is not evidence of good top-two selection.
See [`shorts-live-allstar-mismatch.md`](../bugs/shorts-live-allstar-mismatch.md)
for fresh match comparisons, a one-variable wallbang ablation and real-demo traces.

## Commands

```powershell
python -m cs2archive.shorts.eval_short_score
python -m cs2archive.shorts.eval_short_score --json --output .tmp/shorts-eval.json
python -m cs2archive.shorts.fit_partial_stars --allstar-only
python -m cs2archive.shorts.render_pending_shorts --dry-run
python -m cs2archive.shorts.render_pending_shorts
```

The eval is read-only unless an explicit report output is requested. It never
refits the production artifact or touches a selection ledger. The fit command
explicitly rebuilds `.data/partial_stars.json`. Version-1 artifacts are refused
by the picker because their source/age coefficients were discarded.

## Prediction and evaluation

The shared predictor adds intercept + player + opponent + stage + kinds + source
and clip age. The target is natural log of **positive snapshot views**. Exponentiating
the prediction gives geometric/typical **Allstar** views, not expected arithmetic
views or a forecast of this channel's YouTube views. Production cuts use the
Allstar source and the model's saved reference age, with unknown categories at
zero contribution. Known match stages are read from existing ratings caches.

Evaluation loads Allstar only, rejects clip/enclosing-fixture-ID conflicts before
inheriting context, and deduplicates `(source, clip_id)`. If both snapshot times
are known, the newer wins; otherwise last append wins. Publication time never
orders snapshots. Distinct IDs for the same moment are not merged. Previously
stored scrape timestamps are not treated as confirmed publication timestamps.
Unknown publication ages are retained at age zero and their coverage is reported.

Default evaluation is five-fold, deterministic SHA256 **match-grouped** holdout
(seed `shorts-v2`), refitting only on each fold's training matches. Every usable
clip receives one out-of-fold prediction. Reports include raw-view MSE/MAE/RMSE,
log-view MSE/MAE/RMSE, train-only geometric/arithmetic constant baselines, split
counts and data exclusions. This is label-feature evaluation, not exact
detector-window validation. A non-clutch three-kill cut maps to `3k`; a detector
clutch does not acquire a synthetic `3k` bonus merely from its kill count because
that compound is almost absent in the observed clutch labels. Explicit compound
labels still retain both independent categories. This changes features, not which
moments the detector emits. Player-round union stamps are not mixed in as if they
were clip-window features. No temporal validation claims use scrape dates.

On the 2026-10-05 store: 11,498 positive conflict-free deduplicated clips / 462
matches; 3,086 fixture conflicts rejected, 2,737 duplicate snapshots collapsed,
55 zero-view clips excluded. Model raw MAE 11,061, RMSE 35,061, log RMSE 2.457;
geometric baseline 11,842 / 37,770 / 2.694. This is a modest edge, not a claim of
strong absolute-view prediction. No confirmed publication times are available in
that store snapshot, so the age coefficient has no useful support there.

## Acquisition versus selection

HLTV acquisition retains recognized-pro candidate timelines. It never claims a
daily slot and no longer applies the long-form demand veto, intercept floor or
manual org bonus. Recognition remains required. FACEIT keeps its previous
evidence eligibility policy until clip-view prediction is validated for FACEIT.
Long-form POV scoring and scheduling are unchanged.

The render picker scans the whole pending pool, scores **individual cuts**, and
chooses the highest predictions (deterministic identity ties). Multi-cut legacy
timelines get isolated single-cut outputs when selected. Already rendered videos
and completed/skipped uploads are excluded and preserved. Dry-run prints the
predicted view count, features, exclusions and quota without writing the ledger.

## Daily quota and recovery

`youtube/.shorts_render_selection.json` is the version-2 render ledger. The old
`.shorts_selection.json` extraction claims are intentionally unused. Identity is
the resolved demo path + SteamID + start/end tick bounds. An exclusive process
lock is held throughout the render pass; atomic replacements persist state.

- `pending` → `reserved` just before render → `rendered` after final size and
  1080×1920 resolution checks.
- At most **two successful renders per Australia/Sydney calendar day**. `--limit 1`
  or `--once` lowers one invocation's budget; it cannot increase the daily cap.
- Failed attempts return to pending with an error and do not consume slots.
- Crash reservations survive for retry. If the final output already completed,
  reconciliation records completion using its modification-time Sydney day.
- Unselected pending candidates expire after seven days from first discovery;
  abandoned reservations and missing-timeline entries expire too after their
  completed outputs have been reconciled. `--max-age-days` adjusts this. Expiry
  is recorded/reported, not a file deletion.
- Corrupt/unknown ledger schemas refuse selection rather than reset the quota.
- `--loop` respects the same persisted cap across passes. Direct `render_shorts`
  remains a manual/debug entry point rather than a quota-managed picker.
  Transient render/I/O failures retry on subsequent loop passes; invalid
  model/ledger errors stop the loop with an explicit error.

Uploads remain separate, using the shared CS2UtilArchive **one Short/day** schedule.
The uploader does not reapply the retired HLTV demand/intercept veto. Nothing in
the picker reserves a second upload slot or uploads automatically.

## Implementation verification (2026-10-05)

The same Herdr council reviewed the implementation and fixes: DeepSeek 4.1 Flash,
Muse Spark 1.3, Bunny Alpha and GLM 5.3 Flash all approved. Fixes included completed
multi-cut exclusions, legacy output recognition, opponent fallback, loop retry
accounting, stale crash-reservation expiry and dry-run filesystem suppression.
The five model/data/gate/picker suites pass 75 tests; Ruff and waiver checks pass.
The broader suite is blocked at collection by an unrelated sibling upload-schedule
Windows DST mapping. Excluding only that module produced 1,023 passes, three skips
and the 13 failures already listed in the baseline. The legacy known-failure gate
does not detect that collection error reliably, so its reported OK is not evidence
of a clean full-suite run. No new videos were rendered or uploaded during this work.
