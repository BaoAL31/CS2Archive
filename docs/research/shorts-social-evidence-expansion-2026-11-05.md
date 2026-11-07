# Shorts social evidence — expansion pass (2026-11-05)

Before/after report for the approved data-expansion pass. Interface and schema live
in `docs/agents/social-sweep.md`. Everything here is local, append-only and
public/authorized; no rendering, no uploads, no demo downloads, no paid services,
no OS scheduled task installed.

## Commands run (this pass)

```powershell
$py = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
& $py -m cs2archive.shorts.social_sweep deepen  --config sources.json --max-new 600
& $py -m cs2archive.shorts.social_sweep collect --config sources.json --platform allstar  --no-enrich
& $py -m cs2archive.shorts.social_sweep collect --config sources.json --platform youtube  --no-enrich   # discovery + 600 posts
& $py -m cs2archive.shorts.social_sweep collect --config sources.json --platform instagram --no-enrich
& $py -m cs2archive.shorts.social_sweep enrich  --config sources.json
& $py -m cs2archive.shorts.social_sweep refresh --config sources.json --min-age-hours 1 --max-posts 400
& $py -m cs2archive.shorts.social_sweep report  --config sources.json --out data/social-sweep/coverage_report.txt
& $py -m cs2archive.shorts.social_sweep train   --config sources.json --experimental
```

## Before / after

| | pre-review baseline | after this pass |
|---|---|---|
| posts | 445 | **2,290** |
| metric snapshots | 780 | **2,495** |
| raw responses | 449 | 1,240 |
| identity links | 300 | 1,751 (1,030 exact + 721 hypotheses) |
| feature rows | 9,780 | 51,039 |
| YouTube creators | 1 | **14** |
| verified YouTube Shorts | 23 | **501** |
| Instagram posts / verified Reels | 25 / 25 | **270 / 230** |
| Instagram creators | 1 | 2 |
| Allstar clips | 300 | **1,307** (1,030 with exact staged gameplay) |
| exact gameplay joins (clip↔alignment) | 300 | **1,030** (1,024 measured) |
| distinct matches behind those joins | — | 147 |
| cross-platform same-gameplay groups | 0 | **0** |
| collection runs | 7 | 17 (incl. 7 refresh runs) |

Goals: **≥10 distinct relevant competitor creators** — met (13 competitor
YouTube channels + 1 competitor Instagram account; own channel counted
separately). **≥500 verified short-form posts** — met (501 YouTube Shorts + 230
Instagram Reels = **731**). Goals were not forced: 20 discovered channels were
rejected as dormant (their newest upload predates the collection window), and no
unverified clip was relabelled as a Short.

## (1) Competitor discovery and systematic catalogues

YouTube is the only platform with a permitted public creator-discovery path
(`search.list(type=channel)` → `channels.list` → one uploads page per candidate).
Two discovery runs persisted every candidate with evidence:

* run `discovery-youtube-2026-11-05T2108` — 36 candidates, 12 accepted, 337 units
* run `discovery-youtube-2026-11-05T2139` — 48 candidates, 16 accepted, 449 units,
  20 rejected as **dormant** (`require_recent_activity`)

| YouTube creator | posts | verified Shorts | window covered |
|---|---|---|---|
| RiftFrag — CS2 Highlights | 60 | 60 | 2026-10-12 → 10-28 |
| StrafeTheory — CS2 Highlights | 60 | 60 | 2026-10-14 → 10-28 |
| Counter-Strike Highlights | 60 | 56 | 2026-10-24 → 11-04 |
| CS2 Clips | 60 | 56 | 2026-10-09 → 11-03 |
| BLAST Premier CS2 Highlights | 60 | 48 | 2026-08-31 → 09-08 |
| ESL Counter-Strike Highlights | 60 | 46 | 2026-11-03 → 11-04 |
| StarLadder CS2 Highlights | 60 | 45 | 2026-08-01 → 09-19 |
| NAVI CS2 | 50 | 41 | 2026-08-11 → 11-03 |
| CS2 Daily | 34 | 34 | 2026-09-13 → 10-18 |
| StRoGo CS2 Highlights | 24 | 24 | 2026-09-05 → 09-12 |
| CS2 Archive (own) | 81 | 23 | 2026-09-03 → 11-05 |
| CS2 Highlights by Okko | 60 | 12 | 2026-10-26 → 11-04 |
| CS2 SHORTS | 2 | 2 | 2026-09-02 → 09-12 |
| Wick - CS2 | 2 | 2 | 2026-08-03 → 08-20 |

Per-creator coverage is recorded per run (`collection_runs.notes.creator_coverage`)
with `pages_fetched`, `collected`, `candidates`, `filtered_outside_window`,
`newest_upload_at` and a `truncated_by` label (`per_creator_cap`, `page_limit`,
`collection_window`, `inactive_outside_window`, `full_catalog`). Balanced caps:
60/creator. Low performers are included — the catalogues are contiguous, not
top-N, e.g. `CS2 Highlights by Okko` contributes 12 Shorts out of 60 posts.

Instagram had no discovery path; configured handles were verified with a probe
(`navi.cs2`, `csgoclips`, `counterstrike`, `cs2clips` → `Cannot find user`).
Resolved: `cs2povarchive` (909 followers) and `eslcs` (774,809 followers,
6,592 media). One unresolvable handle is now logged (`creator_unavailable`) and
skipped instead of failing the platform run.

TikTok and Reddit remain **explicitly unavailable** for expansion: TikTok's
cached token lacks `video.list` (metrics) and only oEmbed metadata is permitted;
Reddit publishes no score/comment counts over RSS.

Quota: **517 YouTube Data API units** total this pass (2 discovery runs = 786
units of *their* accounting includes earlier runs; per-run totals are in the
report). The real per-key daily limit is enforced by Google.

## (2) Deepening: exact gameplay joins

`deepen` staged extra Allstar clips **only** from already-cached action timelines
(`data/shorts-foundation/_timelines`, 534 MB, 151 matches) — no demos downloaded,
nothing rendered:

```
staged=700  skipped_existing=60  rejected_no_exact=2  by_kills={3:448, 4:46, 2:186, 1:18, 5:2}
```

Exactness rule (three conditions, all required):

1. the clip's implied kill count (title or Allstar kind, e.g. `4k`, `ace`) equals
   the POV player's demo kills in that round;
2. the clip title names a map (`map_slug_from_title`) and it agrees with the
   candidate timeline's map after normalisation (`de_dust2` / `dust_2` /
   `Dust 2` → `dust2`);
3. **exactly one** cached timeline qualifies.

Condition 3 exists because a multi-map series or a split demo (p1/p2) can hold a
round with the same number and the same kill count on a different map or part.
The first version of this pass matched only `steamid + round + kill count` and
accepted the first hit, so **31 of 600** clips were aligned to the wrong map while
being stamped `confidence: exact` — e.g. `Dust 2 M4A4 2K` aligned to Inferno,
`Cache AWP 3K` to Nuke. All 31 were archived to
`data/shorts-foundation/_archived_map_mismatch/`, and the remaining 569
first-hit picks were archived to `data/shorts-foundation/_archived_pre_mapcheck/`
and re-staged under the fixed matcher so the whole population is map-verified and
ambiguity-free. **0 map mismatches remain** (700/700 new-method alignments carry
`map_verified: true`; the 330 original alignments show 0 title-map conflicts too).

Alignment records `state_missing: true` because `state.parquet` needs demo
parsing, which was not run.

Totals: **1,030 exact Allstar clip ↔ foundation alignments** (1,024 with measured
gameplay), **147 distinct matches**. Feature missingness is explicit
(`post_features.provenance = unavailable`): 24,596 unavailable rows vs 14,334
measured/derived. `first_action_seconds` and `dead_time_seconds` remain
unavailable — no validated measurement exists.

### Cross-platform same-moment links: none exact, deliberately

Captions on YouTube/Instagram/Reddit carry no stable clip id, so no exact
cross-platform link can be built. Instead the sweep records **hypotheses**:

| platform | hypothesis rows |
|---|---|
| allstar | 302 |
| youtube | 250 |
| instagram | 169 |
| reddit | 0 |

Every one is stored `ambiguous = 1` with confidence 0.35 and evidence
(`player_from_caption` / `player_from_channel`, map, kill hint). They never group
and never train. Why unmatched is reported explicitly
(`report.hypotheses.why_unmatched`): captions carry no Allstar clip id, aggregator
channels post without a player identity, or the Allstar clip has no exact staged
alignment. **The dataset still cannot explain cross-platform gameplay success.**

Link rebuilding is now idempotent: derived links are cleared before each enrich
(a changed matcher had left first 1,597 and then a further 1,632 stale rows; that
is fixed and regression-tested).

## (3) Repeated immutable snapshots

`python -m cs2archive.shorts.social_sweep refresh --config sources.json
--min-age-hours N --max-posts M` appends new snapshots for posts older than the
threshold. YouTube (videos.list) and Instagram (re-list + id match) are
refreshable; Reddit (`RSS cannot look up an existing post by id`) and Allstar
(`preserved payloads are immutable`) report `unavailable`, never fake rows.

Actual intervals measured in the live database (never called "24h growth"):

| platform | snapshots | posts with ≥2 snapshots | median interval | min / max |
|---|---|---|---|---|
| youtube | 853 | 131 | 9.145 h | 0.526 h / 9.449 h |
| instagram | 295 | 25 | 9.054 h | 9.054 h / 9.054 h |
| allstar | 1,200 | 0 | — | — |
| reddit | 40 | 0 | — | — |

The refresh CLI wrote 21 new YouTube snapshots live; subsequent invocations
correctly no-op under the age threshold. Recurring wrapper
`cs2archive/shorts/social/refresh_social.ps1` is provided and documented but
**not installed** as a scheduled task or daemon (needs explicit approval).

Bug fixed this pass: `collection_runs.mode` was written as `'collecting'` because
of a placeholder/column mismatch; `refresh` runs are now labelled correctly and
the pre-fix labels were repaired by an idempotent migration.

## (4) Training population

### YouTube / Instagram — refused (honest)

All 501 verified YouTube Shorts and 230 Instagram Reels have **no exact clip
identity** (only ambiguous hypotheses), so the scored set is verified-only and
empty:

```
[youtube] refused  only 0 usable rows with verified_only identity (minimum 25)
[instagram] refused  only 0 usable rows with verified_only identity (minimum 25)
[reddit]/[tiktok] refused  no comparable exposure metric on those paths
```

No gate was weakened to manufacture a result. What would unlock a fit: a
verified identity path for competitor posts (e.g. an authorized uploads API that
exposes source media ids), or self-published Shorts joined to their rendered
`short_timeline.json` (only 2 `upload_meta.json` files exist locally today).

### Allstar gameplay ranking — separate, match-held-out, date-free

`train --experimental` → `[allstar_gameplay] fitted`. Label is **Allstar views at
import time**; there is no publication date, so there is no date-based validation
and the result explicitly does not forecast YouTube/Instagram.

After the map fix and the additional staged alignments (numbers below are the
current, post-fix run):

```
split: train=751  test=273  train_matches=111  test_matches=35
       method=match_group_hash_80_20   (identical groups never straddle the split)
baseline_global_median    mae=2.2904  spearman=None
baseline_creator_median   mae=2.7808  spearman=-0.04
ridge:all                 mae=2.1812  spearman=0.3262  p@k=0.2
hgb:all                   mae=2.3085  spearman=0.2377  p@k=0.2
ridge:no_headshots        mae=2.1718  spearman=0.3389  p@k=0.0
hgb:no_headshots          mae=2.2976  spearman=0.2310  p@k=0.2
ridge:no_weapons          mae=2.1761  spearman=0.3238  p@k=0.2
hgb:no_weapons            mae=2.2743  spearman=0.2350  p@k=0.2
ridge:density_only        mae=2.1906  spearman=0.3381  p@k=0.4
hgb:density_only          mae=2.3260  spearman=0.1748  p@k=0.2
```

Pre-fix, for comparison, the same experiment reported
`baseline_creator_median mae=7.5837 spearman=None`. That baseline was invalid:
the label is **absolute** `log1p(views)` and unseen creators fell back to `0.0`
(= "zero exposure"), which is a fabricated prediction. The creator baseline now
falls back to the **training-wide median** for unseen creators, is reported with
its `fallback`/`fallback_value`/`fallback_count`, and is omitted entirely when the
creator feature is absent from training or held-out rows. On the cohort-relative
YouTube/Instagram target the equivalent fallback stays `0.0` because the target is
already a deviation from the cohort median; that baseline now says so explicitly.

The corrected creator baseline (2.78) is *worse* than the global median (2.29),
and its rank correlation is ~0, so per-creator means carry no usable signal on
held-out matches. Both models still beat both baselines on MAE with positive rank correlation, but the honest reading is that the effect is modest
and the label is Allstar-views-at-import, not a YouTube forecast.

Residual modelling fixes in this pass:

* zero engagement is **kept** as data (target `log1p`, declared) instead of being
  dropped as unusable;
* cohort medians are fitted on training rows only, with an explicit
  `test_cohort_fallbacks` count and a warning that those rows are not fully
  age-normalised;
* `age_hours_at_observation` is stored for audit but is excluded from every
  predictor; a residual-vs-age diagnostic is reported alongside `ridge:all`;
* baselines are scale-correct: the absolute-scale creator baseline uses the
  training-wide median for unseen creators (never `0.0`), and the cohort-relative
  baseline documents its `0.0` prediction explicitly;
* unknown feature values get explicit `<feature>__missing` indicators rather than
  reading as an observed zero;
* all preprocessing (vectorizer, scaler) is fit on training rows only;
* the arbitrary "80% identity coverage" gate was replaced: the scored set is
  restricted to verified identities, and the residual leakage risk is labelled
  (`identity.mode = verified_only | unverified_risk`) when the operator opts in;
* baselines (cohort median, creator median, global median) and tie-safe
  average-rank Spearman / deterministic precision@k are reported for both
  experiments.

## (5) Honest limitations

* **One alignment bug was found and fixed during review**: `deepen` originally
  matched only `steamid + round + kill count`, so 31 clips were aligned to a
  round on the wrong map of the same series while stamped `exact`. The matcher now
  requires title-map agreement and rejects ambiguous multi-timeline matches; the
  affected clip dirs were archived (no silent delete) and re-staged.
* **No cross-platform same-gameplay groups** (0). The store cannot yet explain
  cross-platform gameplay performance; 702 ambiguous hypotheses are preserved and
  reported instead of being passed off as exact.
* **Competitor posts have no exact gameplay join.** Deepened gameplay exists only
  for Allstar clips (930 exact), which have no publication dates.
* **Allstar label is not comparable to YouTube views** and is not used as if it
  were; the two experiments are separate and never pooled.
* **TikTok is blocked** (token scope) and **Reddit exposes no counts**; both
  remain explicit rather than approximated.
* **Instagram exposes likes/comments only** — no plays, shares, saves or
  duration, so its target is a documented proxy.
* 21 YouTube videos have `unknown` format (probe returned an unmarked response);
  151 are long-form. Both are excluded from the short-form counts.
* Interview/analysis channels and non-CS2 content inside accepted channels are
  included as-is (catalogue completeness over curation); only whole channels are
  relevance-gated.
* The 600-post YouTube cap and 60/creator cap were both hit, so the very oldest
  uploads in the window are not yet collected — `page_limit`/`per_creator_cap`
  labels record this per creator.

## Left untouched

`allstar_selector.py`, the picker, rendering, uploads, demos, avatars and Steam
were not modified. No commits. Unrelated dirty work preserved.
