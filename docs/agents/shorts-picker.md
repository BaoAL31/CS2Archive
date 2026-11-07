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

### Uploading is all four platforms, unless an operator says otherwise

`upload_youtube_shorts.py` publishes one Short to **YouTube, TikTok, Instagram and
Facebook** in a single run. That is the default and the product contract: a
YouTube-only Short is invisible on three of the four channels.

The per-platform opt-out flags (`--skip-tiktok` / `--skip-instagram` /
`--skip-facebook`) still exist for a genuinely broken platform, but they are
**hidden from `--help` and inert unless `CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD=1` is in
the environment**. Passing one without it exits 2:

```
[ERROR] refusing a partial upload (--skip-tiktok).
  A Short is published to YouTube, TikTok, Instagram and Facebook; dropping
  a platform is an operator decision, not an agent one. Ask the user first.
  If they approve, re-run with CS2ARCHIVE_ALLOW_PARTIAL_UPLOAD=1 in the environment.
```

This gate exists because an agent used the flags on its own initiative to ship a
YouTube-only Short (2026-11-07). **Do not set the env var to get past an error** —
ask the user. A broken TikTok session is fixed at the source (`python
scripts/tiktok_api.py login` in CS2UtilArchive), not by narrowing the upload.

- Each platform runs in its own `try/except`; a failure logs `[<platform>][FAILED]`,
  the remaining platforms still complete, and the process exits **non-zero** with
  `[ERROR] cross-post incomplete: …`.
- An authorized skip is recorded in the meta as `skipped_platforms: [...]`, so
  `upload_pending_shorts.py` treats that platform as settled instead of retrying
  it forever, and an audit can find every partial upload.
- Recovery from a failure is a plain re-run: YouTube is skipped when
  `upload_status=completed` + `youtube_id` are set, and each social platform is
  skipped once its own status field is terminal. Only the missing platforms retry.

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

## Allstar pool and the LLM picker (2026-11-06)

The view-prediction picker above ranks cuts that already exist. The Allstar
picker is a different stage: it decides **which Allstar clip is worth cutting**
in the first place, from the last week of pro matches.

```powershell
python -m cs2archive.shorts.allstar_pool --scrape            # last 14 days of matches
python -m cs2archive.shorts.allstar_pool --refresh           # re-poll views, no browser
python -m cs2archive.shorts.allstar_pool --pool --limit 25   # eligible clips
python -m cs2archive.shorts.picker_nominations --days 7 --limit 5
python -m cs2archive.shorts.picker_agent --nominations .data/allstar_picker/nominations_<date>.json
```

### Why a snapshot store exists

`.data/allstar_hltv_probe.jsonl` is **upsert-per-match**: re-scraping a match
replaces its row, so the previous view count is destroyed. That is why the store
held 28k clips with not one second snapshot — no view rate was computable.
`allstar_pool` adds `.data/allstar_view_snapshots.jsonl`, append-only and
idempotent per `(clip_id, observed_at)`, so history survives a re-scrape.

### Publication time is known after all

The playlist payload carries `createdDate` and `clipLength`, which
`clip_from_allstar` now keeps as `published_at` / `clip_length` (plus
`score_line` and `series_game_label` from the partner metadata). So a clip's
**real age** is known, and `views_per_hour` is lifetime views over that age —
age-normalised and comparable across clips from a single snapshot.
`recent_views_per_hour` is growth between snapshots and stays null until two
snapshots are at least 6h apart; a five-minute gap measures noise, not a rate.

### Eligibility

- **Age cap** `--days` (default 7) from the match date. The match date comes
  from the HLTV match page (`data-unix` in the header), recorded as
  `match_date` at scrape time; the fallback is the earliest clip publication
  time in the row. It is deliberately **not** the snapshot time or `scraped_at`
  — those say when we looked, and using them makes a month-old clip look new.
- **Already rendered** is excluded via `rendered_clip_ids()`: `clip_identity_links`
  rows with `method='render_upload_linkage'` (our own uploads) plus
  `short_linkage` evidence carrying `allstar_clip_id` (staged renders).
- Non-popular events are dropped, matching `load_allstar_dataset`.

### Incremental scrape and refresh

The scrape window is a **fixed 14 days** (`--scrape-days`, default 14) and the
scrape runs **every 2 days**, so consecutive runs overlap by 12 days. The
overlap is deliberate: CS2 has 2-3 week stretches with no pro matches at all,
and a window that only looked at the last 2 days would find nothing during a
break and could miss the resumption. Re-covering the whole fortnight is cheap
because every match id already in the probe store is skipped — only genuinely
new fixtures cost a match-page request.

`--scrape` paginates `/results?offset=N` (100 per page, capped at 12 pages)
until the oldest `.standard-headline` group on a page predates the window
start, no new match ids appear, or the page cap is hit. One page does not cover
two weeks of pro matches, which is why the loop exists. Matches are then
filtered to the window by headline date, non-popular events are dropped, and
only the survivors get a match-page visit.

`--refresh` needs no browser and no HLTV: the playlist API is keyed by the
stored `playlist_id`, so it is one HTTP call per match, and the probe rows are
rewritten once at the end. A 404 means Allstar expired the playlist; the row is
stamped `playlist_dead_at` so it is not re-asked forever.

Eligibility stays at 7 days, so the 14-day scrape window deliberately
over-fetches: the extra week is what keeps the pool populated when the picker
runs a day late, and `rendered_clip_ids()` drops anything already cut.

### The picker agent

The shortlist is **five clips** (`--limit`, default 5): the most-viewed eligible
clips, ranked by raw `views`. One Short is published per day and the scrape runs
every 2 days, so the picker only ever needs to choose one clip per run — 25
candidates turned a decision into a research project, and the bake-off showed
models spending their budget paging a long list instead of reading the facts of
the few clips that mattered. Five is small enough that the prompt tells the
agent to open **every** facts file rather than skim the index.

Views is a popularity prior, not a verdict: it is the entry filter, and the
agent's job is to decide which of the five actually cuts into a better Short.
Note that raw views favours the oldest clips in the window (a clip published 6
days ago has had 6x longer to accumulate than one published today), so the
shortlist skews old within the 7-day eligibility cap. `views_per_hour` is on
every nomination as the age-normalised counterweight.

`picker_nominations` writes **two** things, and the split is the whole point:

- `nominations_<date>.json` — a pageable **index**, one entry per clip with a
  `facts_summary` (kill count, headshots, span, gaps, buy, round result,
  weapons, through-smoke/wallbang counts, series result). ~60 KB, 2.2k lines,
  longest line under 400 bytes.
- `facts/<clip_id>.json` — the full per-kill detail, ~3.5 KB each, opened only
  for the clips actually being weighed.

Both shapes were forced by observed failures. A single pretty-printed file was
167 KB and the slowest model spent its whole budget paging through it 1100 lines
at a time instead of researching. A single **compact** file is one 70 KB line,
and the agent's `read` tool refuses any line over 50 KB — it returned a 103-byte
error and the model then flailed: `fetch_content` on `file://`, guessed
filenames, a probe of `127.0.0.1:8000`, and a web search for "unknown tool test".
Never emit a line over 50 KB to this agent.

`alive_counts_over_round` and `hot_zones` are dropped: 10 KB of per-round arrays
the prompt never mentions.

`picker_agent` runs Pi headless with research tools only (no bash/edit/write),
streams the transcript to disk so a killed run still leaves evidence, and
parses the trailing fenced JSON block. Every clip id is validated against the
nominations it was given — an invented id is dropped, never queued.

`allstar_selector.order_by_pick` applies the result to the render queue: the
agent's order wins for the clips it ranked, unranked clips keep their view
order after them, and a pick naming a clip outside the pool is ignored.

### Model bake-off

Without `demo_facts` all four models ranked narrative ("grand final",
"underdog") and got in-game facts wrong — one claimed a 13-2 map that was 13-6,
another an elimination loss that was a 2-1 series win. With the facts they
ranked gameplay (kill span, headshots, gaps, buy type, round won) and their
numbers checked out. **Do not ship a picker without the facts layer.**

`deepseek-v4.1-flash` is still not a candidate, but the 3600s timeout was a
payload symptom, not a model verdict. Three runs, same nominations:

| Payload | What happened |
|---|---|
| 167 KB pretty-printed, one file | 5 `read` calls, 25 min, no research, killed at 3600s |
| 73 KB compact, one file | `read` refused: "Line 1 is 71.6KB, exceeds 50.0KB limit". The model then flailed — `fetch_content` on `file://`, guessed filenames, a probe of `127.0.0.1:8000`, a web search for "unknown tool test" |
| 60 KB index + 3.5 KB facts files | 2 index reads, 6 targeted facts reads, quoted real numbers ("kill_span 46.14s, gaps [1.25, 36.06, 8.83], full vs force, won, 2 HS"), then moved to research |

The third run then stalled on the provider for 15+ minutes with no output and
the agent process died. That is an nvidia-nim reliability problem, separate from
the payload, and it is why `glm` (also nvidia-nim) and the two OpenRouter models
are the production candidates.
