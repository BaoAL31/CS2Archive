# Social evidence sweep (cross-platform Shorts database)

Cross-platform, evidence-first database of CS2 short-form clips plus an
experimental model sweep. It exists to answer "why do some clips perform
differently" with measured evidence instead of hand-tuned penalties.

```powershell
python -m cs2archive.shorts.social_sweep collect --config sources.json [--platform X] [--no-enrich]
python -m cs2archive.shorts.social_sweep deepen  --config sources.json [--max-new N]
python -m cs2archive.shorts.social_sweep enrich  --config sources.json   # relink + refeature
python -m cs2archive.shorts.social_sweep refresh --config sources.json [--min-age-hours N] [--max-posts M]
python -m cs2archive.shorts.social_sweep report  --config sources.json [--json] [--out FILE]
python -m cs2archive.shorts.social_sweep train   --config sources.json --experimental
```

| Command | What it does |
|---|---|
| `collect` | Bounded platform sweep; runs YouTube creator discovery first when `platforms.youtube.discovery.enabled` |
| `deepen` | Stages extra Allstar clips from **already-cached** action timelines (no demos, no rendering); exact kill-count match only |
| `enrich` | Rebuilds derived identity links (stale hypotheses cleared) and rewrites feature rows |
| `refresh` | Appends new immutable metric snapshots for posts older than the threshold |
| `report` | Coverage, snapshots/intervals, hypotheses, quota, eligibility, interpretation |
| `dossiers` | Build LLM-readable moment dossiers from local evidence |
| `linkage` | Inspect/resume the durable short → demo linkage store |
| `linkage --resolve` | Re-attempt `pending` rows from the durable mirror (purge recovery) |
| `backfill` | Verify + link own-channel Shorts to local demos |
| `analytics` | Ingest authorized own-channel YouTube Analytics (separate family) |
| `ceiling` | How much of the corpus can ever be demo-linked, and what cannot |
| `train` | Strict per-platform sweep **plus** the separate `allstar_gameplay` experiment |

Code: `cs2archive/shorts/social/` (store, adapters, features, identity, report,
train) with `social_sweep.py` as the CLI. Database:
`data/social-sweep/shorts_evidence.sqlite3`; raw responses under
`data/social-sweep/raw/<platform>/<run_id>/`.

## Access status (measured 2026-11-05, not assumed)

| Platform | Status | Path that works | Not available on this path |
|---|---|---|---|
| YouTube | **supported** | Data API v3: `channels.list(forHandle)` → uploads playlist → `videos.list` batches; `GET /shorts/<id>` route probe proves format | shares, saves, plays, impressions, CTR, any historical "views at 24h" |
| Instagram | **partial** | Graph Business Discovery (`/{ig-user-id}?fields=business_discovery.username(...)`) for public business/creator accounts | plays/views, shares, saves, video duration |
| Reddit | **partial** | public subreddit Atom RSS (`/r/<sub>/new.rss`) | score, comment count, all metrics; `/new.json` returns 403; an existing post cannot be re-fetched by id, so `refresh` reports `unavailable` |
| Allstar | **partial** | import of preserved probe payloads (`.data/allstar_hltv_probe.jsonl`) or an authorized export | live reads: `prt.allstar.gg/playlist` → `403 AUTHENTICATION FAILED`; `allstar.gg/clip/<id>` is an SPA shell |
| TikTok | **blocked** | oEmbed for an explicit public video URL (metadata only); authorized account export import | metrics: needs Display API `video.list` scope (cached token is `user.info.basic,video.upload`) or an approved Research API application |

Statuses are **not** embellished: a blocked platform writes a `collection_runs`
row with status `blocked` plus a `collection_errors` row listing the required
credential, and zero posts.

## Creator discovery (YouTube only)

`platforms.youtube.discovery` = `{enabled, queries, max_creators, min_relevance,
require_recent_activity}`. Each run searches `type=channel`, resolves channels,
samples one uploads page per candidate, scores by title relevance (+25 when the
newest upload is inside the window) and persists **every** candidate to
`discovered_creators` with its evidence (recent titles, `newest_upload_at`,
`active_in_window`, subscribers). Runs are recorded in `discovery_runs` with
`rejected_low_relevance`, `rejected_own` and `rejected_dormant` counts.

Instagram/TikTok/Reddit have no permitted creator-search path: they are
handle-configured only. An unresolvable Instagram handle is logged
(`creator_unavailable`) and skipped — it never fails the platform run.

## Deepening (exact gameplay joins)

`deepen` reads `.data/allstar_hltv_probe.jsonl` plus the cached
`data/shorts-foundation/_timelines/` action timelines and stages
`clips/<clip_id>/` only when **all three** hold:

1. the clip's implied kill count (title or kind, e.g. `4k`/`ace`) equals the POV
   player's demo kills in that round;
2. the title names a map (`map_slug_from_title`) and it agrees with the candidate
   timeline's map after normalisation (`de_dust2`/`dust_2`/`Dust 2` → `dust2`);
3. exactly **one** cached timeline qualifies.

Rule 3 matters because a multi-map series or a split demo (p1/p2) can contain a
round with the same number and the same kill count on a different map or part;
accepting the first hit produced 31 wrong-map alignments stamped `exact`. A title
without a map is never stamped `exact`. Rejections are counted as
`rejected_no_expected` / `rejected_no_map` / `rejected_map_mismatch` /
`rejected_no_exact` / `rejected_no_round` / `rejected_ambiguous`, and each
alignment records `map_verified`, `title_map` and `timeline_map`. No demo is
downloaded and `state.parquet` is not written
(`alignment.state_missing = true`).

Identity is evidence-tiered and never inflated:

| tier | example | groups/trains? |
|---|---|---|
| exact | `clip_identity_links.method = allstar_clip_id`, confidence 1.0 | yes |
| hypothesis | `caption_player_map_kills_hypothesis` (player/map/kills from the caption or channel name), confidence 0.35 | **no** — always `ambiguous = 1` |

Derived links are cleared and rebuilt on every `enrich`, so a changed matcher
cannot leave stale rows behind. `report.hypotheses.why_unmatched` names the reason
a post is unmatched (no stable clip id in caption / no player-or-map signal /
Allstar clip not staged).

## Durable short → demo linkage (go-forward instrumentation)

A rendered Short's evidence lives in the render directory, which is purged after
upload. Two hooks now persist it first, both **instrumentation only** (never
changing rendered bytes or selection ranking, never fatal, retries capped at 3,
`CS2ARCHIVE_NO_LINKAGE=1` disables):

* `render_shorts._record_linkage` — after the meta step, stages the evidence from
  `short_timeline.json` (demo path, match id, map, player steam id/nick, round,
  tick span, kill ticks, `short_type`, `source`, AllStar clip id, selection
  provenance) into the `short_linkage` table keyed by a deterministic `short_id`,
  writes `linkage.json` beside the mp4, and mirrors it to
  `data/social-sweep/linkage/<short_id>.json`.
* `upload_youtube_shorts._record_linkage` — once a platform post id exists,
  `confirm_linkage` flips the row to `linked`, mirrors
  `linkage/<platform>/<post_id>.json`, and adds an exact
  `clip_identity_links` row (`method = render_upload_linkage`, confidence 1.0) so
  the trainer's verified-only set can use it.

Idempotent (same evidence + status rewrites nothing) and resumable
(`linkage --resolve` re-attempts `pending` rows from the durable mirror after a
purge). Statuses: `pending` → `linked`, or `unresolved` with a reason.

## Own-channel backfill (verified links only)

`backfill` consumes surviving authoritative evidence **first**, then falls back
to title inference:

1. **Render-selection ledger** (`youtube/.shorts_render_selection.json`) — the
   render directory name encodes the demo stem and player
   (`renders/pov-<demo-stem>_<player>/shorts/...`), which is real provenance, not
   a title. Each entry is matched to a local demo by stem, its kill count/tick is
   verified against that demo's action timeline, and the record is staged
   `pending` — the ledger holds no platform post id, so the reason says so
   instead of guessing.
2. **Title inference** — the player comes from the pipeline's own
   `.data/player_accounts.json` registry (nick / FACEIT nick / steam vanity), the
   map and kill signature from the title, and candidates are verified against
   **action timelines of local demos** restricted by map, player, opponent (when
   named) and a publish-date plausibility window.

A consequence worth stating: the window is directional (a demo cannot be local
*before* the match it contains, and a demo whose mtime post-dates publication by
more than the allowance is rejected), so tournaments whose demos are no longer on
disk cannot be back-linked at all, however clear the title.

A link is `linked` only when **exactly one** round qualifies:

| signature | requirement |
|---|---|
| `ace` / `5k` | exactly 5 kills by that player in the round |
| `Nk` | exactly N kills |
| `1vX` | X kills **and** the player's team won the round |

Outcomes are stored explicitly: `ambiguous` (with every candidate + reason),
`no_candidate`, `no_map` (title names no map — a title alone is never accepted),
`no_demo`. Only `linked` creates an identity link. Demos are read-only; timeline
building is bounded by `--build-budget` and failures are logged, not fatal.

### Measured outcome (2026-11-06)

Live run: 2 ledger entries verified and staged `pending`, 2 `ambiguous`,
346 `no_candidate` (327 titles carry no kill signature), 27 `no_map`, and **0
exact title-inferred links** — the local demo corpus (ESL Pro League S24 / PGL
Bucharest / FACEIT, Oct 2026) does not contain most of the matches those Shorts
came from. Numbers and the two wrong links that the opponent rule now rejects:
`docs/research/shorts-demo-linkage-2026-11-06.md`.

## Own-channel completeness

`platforms.youtube.own_max_pages` (default = `max_pages`) paginates the home
channel's uploads completely, and the home channel is exempt from both
`per_creator_max` and the collection window — its catalogue is the reference
population for our own analytics. Competitor catalogues stay bounded by
`max_pages` / `per_creator_max` so the quota cost of completeness is not paid for
rivals. `collection_runs.notes.creator_coverage[].own_channel` records which is
which.

## Own-channel analytics (separate metric family)

`analytics` reads `token_youtube_analytics.json` (scope
`yt-analytics.readonly`, created by
`python -m cs2archive.upload.auth_youtube_analytics`) and stores per-video
windows in `analytics_snapshots` with `metric_family = youtube_analytics`, the
window recorded (first full Pacific day after publication for 7 days, same rule
as `export_pov_analytics`), and append-only triggers.

* It is **never** read by the public-views target loader
  (`train.load_platform_rows` reads `metric_snapshots` only), so impressions/CTR/
  retention cannot leak into or be mixed with the public label.
* An empty report means *unavailable*, never zero.
* A dead/expired credential fails fast: one `analytics_auth_failed` error, no
  per-video hammering, and the run reports the re-auth command.
* Missing token/scope → clean refusal with the exact remediation, and no rows.

## Moment dossiers (LLM-readable demo representation)

```powershell
python -m cs2archive.shorts.social_sweep dossiers --config sources.json [--limit N] [--force]
python -m cs2archive.shorts.social_sweep dossiers --config sources.json --plan-state
python -m cs2archive.shorts.social_sweep dossiers --config sources.json --enrich-state 40
```

One dossier per **moment** (demo + round + player + kill window), written as
`data/shorts-foundation/dossiers/<moment_id>.json` plus a `.txt` rendering and an
`INDEX.json` with aggregate availability. Several AllStar clips describing the
same moment share one document (their clip ids are listed), so nothing is
overwritten.

Built **only** from local evidence: the cached action timeline, the staged clip
folder (`alignment.json`, `events.parquet`, `state.parquet` /
`state_enriched.parquet`) and the `short_linkage` records.

| Section | Contents |
|---|---|
| `provenance` | demo path, match id, map, tickrate **and its source**, timeline/event-data/stakes versions, place source, alignment method + confidence, state source |
| `bounds` | kill window (explicitly `kill_anchored_inferred`), kill ticks, round bounds, and `published_clip_bounds.known = false` with the reason |
| `clip_verification` | always `verified: false` — AllStar map+player+round+kill-count alignment is *plausible evidence, not proof* of the published clip's contents |
| `identity` | player steam id/nick, team number, T/CT side (from `sides_by_round`), team + opponent org slugs from the fixture folder, pro flag, all team steam ids |
| `round_context` | round bounds/duration, winner + win reason, score after, match point/overtime/closes-map, hot zones, `stakes` (buys, equip value, win streak, deficit, pistol flag, buy source), man-advantage counts at the first kill, the player's own round stat line |
| `pacing` | window seconds, kills, kill span, time to first kill, dead time after the last kill, shots in window, shots per kill |
| `player_state` | only when a snapshot exists: sample count, X/Y/Z ranges, health range, yaw/pitch range, weapon indices, and the snapshot file it came from |
| `chronology` | tick-ordered events (damage, kill, shot, utility thrown/damage, bomb) with flags (headshot, wallbang `penetrated`, thrusmoke, noscope, attackerblind, dominated/revenge), weapons both sides, hitgroup, distance, places, victim health/armor after |
| `availability`, `unknowns` | which feature families are genuinely present, and the explicit not-measured list (LOS, intent, ammo, movement speed, audio, published trim, per-opponent loadout beyond each opponent's own death weapon) |

Rules: nothing is inferred (no LOS, no intent, no reconstructed round state);
missing fields read as `null`/`false`, never as zero; the canonical round-context
keys always exist. Writes are atomic (temp + `os.replace`) and idempotent — a
dossier whose `source_hash` (identity + bounds + source versions + state source)
is unchanged is skipped unless `--force`.

`--enrich-state N` parses up to N local demos with demoparser2 for moments whose
window has no state snapshot and writes a **new**
`clips/<id>/state_enriched.parquet`; `state.parquet`, `events.parquet`,
`alignment.json` and the timelines are never modified. Adding a snapshot changes
the `source_hash`, so the next `dossiers` run rewrites just those moments.

## Demo-linkage ceiling

`ceiling` reports, per platform, how many posts are `linked_exact`, hypothesis-only,
title-feasible, evidence-feasible against the local demo corpus, and permanently
unlinkable — plus the competitor unlinkable share. Public titles carry no stable
clip id, so a competitor Short can only ever be linked when its title names a map
**and** a kill signature **and** a local demo for that player+map exists. The
eligible count for gameplay modelling is derived from the same exact links the
trainer uses.

## Metric refresh

`refresh` appends a new snapshot for posts whose newest observation is older than
`--min-age-hours`. YouTube (`videos.list`) and Instagram (re-list + id match) are
refreshable; Reddit and Allstar report explicit `unavailable` reasons. Snapshot
intervals are reported as measured spacing (`report.snapshots`), never as a
reconstructed 24h growth.

Recurring wrapper: `cs2archive/shorts/social/refresh_social.ps1`
(`-MinAgeHours`, `-MaxPosts`, logs to `data/social-sweep/refresh.log`). It only
runs the bounded CLI — **no daemon and no scheduled task is installed**; the
`schtasks` line in its header is documentation to run after asking.

## Credentials

Read from the existing `.env` via `cs2archive.config.settings` (never printed or
persisted):

* `YOUTUBE_API_KEY` — YouTube Data API v3 read key.
* `META_SYSTEM_USER_TOKEN` (or `META_PAGE_ACCESS_TOKEN`) + `META_IG_USER_ID` —
  Graph API, scopes `instagram_basic`, `pages_read_engagement`.
* Reddit RSS and Allstar imports need no credential. Allstar live reads and
  TikTok metrics do (see table).

Secrets are scrubbed before anything is written: raw payloads are recursively
redacted for the configured secret values, and request provenance stores parameter
names only (`key=[REDACTED]&id=abc`).

## Config (`sources.json`)

```jsonc
{
  "database": "data/social-sweep/shorts_evidence.sqlite3",
  "raw_root": "data/social-sweep/raw",
  "window": {"start": "2026-08-01T00:00:00Z", "max_days": 120},
  "rate_limit": {"min_interval_seconds": 1.5, "max_requests_per_run": 120,
                 "youtube_daily_quota": 5000},
  "retry": {"max_attempts": 3},
  "platforms": {
    "youtube":   {"enabled": true, "mode": "api", "creators": ["@cs2povarchive"],
                  "video_ids": [], "max_pages": 2, "max_posts": 900,
                  "own_max_pages": 10,
                  "per_creator_max": 60, "own_channel_id": "UCh8PPT...",
                  "discovery": {"enabled": true, "queries": ["cs2 shorts highlights"],
                                "max_creators": 16, "min_relevance": 0.5,
                                "require_recent_activity": true}},
    "instagram": {"enabled": true, "mode": "graph_business_discovery",
                  "creators": ["cs2povarchive", "eslcs"], "max_pages": 8, "max_posts": 220},
    "reddit":    {"enabled": true, "mode": "rss", "communities": ["GlobalOffensive"],
                  "min_interval_seconds": 8.0, "max_posts": 40},
    "allstar":   {"enabled": true, "mode": "local_store_import",
                  "store": ".data/allstar_hltv_probe.jsonl", "max_posts": 300},
    "tiktok":    {"enabled": true, "mode": "oembed", "video_urls": []}
  },
  "imports": [{"platform": "tiktok", "kind": "authorized_export",
               "path": "exports/tiktok.json"}],
  "foundation": {"dir": "data/shorts-foundation"},
  "training": {"min_rows_per_platform": 25, "output": "data/social-sweep/train_report.json"}
}
```

Per-platform `min_interval_seconds` / `max_requests_per_run` override the global
rate limit (Reddit RSS throttles hard). `per_creator_max` balances the catalogue
across creators; `own_channel_id`/`own_channel_handle` keep the home channel out
of the competitor count. `training.allow_unverified_groups: true` opts into
scoring unverified identities and labels the residual risk `unverified_risk`.
`imports` turn a platform into an authorized-export import and never mix with live
API rows (metric source differs). `own_max_pages` bounds the home channel's
complete-history pull and exempts it from `per_creator_max` / the window.

## Database

| Table | Purpose |
|---|---|
| `posts` | `(platform, post_id)` identity, creator, URL, caption, `published_at`, `first/last_observed_at`, duration, `format_label` + `format_confidence` + `format_evidence` |
| `metric_snapshots` | append-only observations; views/plays/likes/comments/shares/saves/followers (**NULL when unavailable**), `metric_source`, `unavailable` list, raw-response id. BEFORE UPDATE/DELETE triggers abort |
| `raw_responses` | append-only provenance index: endpoint, sanitised params, HTTP status, fetch time, SHA-256, byte size, path |
| `snapshot_provenance` | additive snapshot → raw-response backlinks (used when a re-imported observation keeps the same snapshot id) |
| `collection_runs` | one row per platform attempt: mode, status (`ok`/`partial`/`blocked`/`unavailable`/`failed`), window, pages, posts, snapshots, quota units |
| `collection_errors` | every failed attempt (attempt number + max) and terminal failures |
| `clip_identity_links` | post → foundation clip / match with `map_name`, `player`, `round_no`, method, confidence, `ambiguous` flag |
| `post_features` | long-format features: `kind` (gameplay/presentation/context), `provenance` (measured/derived/imported/unavailable), confidence, evidence |
| `quarantines` | posts excluded from training/nomination with reason |
| `authorized_imports` | export file path + SHA-256 + rows read/kept |
| `dossiers/` (on disk) | one JSON + TXT per moment, plus `INDEX.json` with availability counts |
| `short_linkage` | durable short → demo record: demo path, match id, map, player, round, tick span, kill ticks, `short_type`, source, selection provenance, status |
| `analytics_snapshots` | owner-only windowed analytics (separate family, append-only): impressions, CTR, average view duration/percentage, watched minutes, subscribers |

### Format evidence rules

* **YouTube Shorts**: `GET https://www.youtube.com/shorts/<id>` with redirects
  disabled. Verified Short requires **200 plus a Shorts marker** (canonical
  `/shorts/<id>` link or `"isShortsEligible":true`), confidence 0.98. A redirect
  to `/watch?v=<id>` verifies long-form (0.95). A 200 without a marker
  (consent/interstitial/anything) or an unrelated redirect is **unknown**, never
  Short. Duration alone yields `candidate_short` at 0.4 and is excluded from
  training.
* **Instagram**: `media_type=VIDEO` + `/reel/` permalink → verified (0.9); plain
  video `clip` (0.6, unverified).
* **Reddit**: outbound link host (Short/Reel/TikTok/clip host) — evidence about
  the *linked* clip, not the Reddit post.
* **Allstar import**: `clip` (0.4) — aspect/format not independently verified.
* **TikTok oEmbed**: vertical embed dimensions (0.85); no publish time, no metrics.

## Features

Gameplay/presentation features come only from a **verified** link (unambiguous,
confidence >= 0.8) to a staged foundation clip whose `alignment.json` confidence
is `exact`. Partial/unresolved alignments, quarantined clips, ambiguous links and
posts with two verified clips get `provenance = unavailable` rows instead of
measured values. Feature confidence is the alignment's confidence, never a
blanket 1.0.

Features: kills, headshots, headshot rate, distinct weapons, kill span,
kills/10s, `fire_events`, `hit_events`, `hit_event_ratio` (hit *events* per
firing event — explicitly **not** validated shot accuracy), damage, multikill,
clutch size, clip/round span, round number. Utility weapons are excluded from
fire/hit counts, and a missing headshot flag makes `headshot_rate` unavailable
rather than False. `first_action_seconds` and `dead_time_seconds` stay
unavailable (no validated measurement). `age_hours_at_observation` is stored for
audit but is never a model input.

Identity: exact platform clip id (1.0) → clip id in caption (0.95, `ambiguous`
when several) → player+map+round fuzzy (0.35, always excluded from grouping).
Quarantined foundation alignments are linked for audit but never measured.

## Experimental training

```powershell
python -m cs2archive.shorts.social_sweep train --config sources.json --experimental
```

* Refuses unless `--experimental` is passed, and refuses per platform with a
  coverage reason when there are not enough verified rows.
* **Verified short-format rows only**: `format_label = short`, confidence
  >= 0.8, and `format_evidence.verified = true`. Long-form, `candidate_short`,
  Allstar clip artifacts and export rows never train. This is the strict path and
  it **excludes the whole Allstar population by design** — Allstar clips are
  `format_label = clip`, not a verified vertical Short, so they are handled by the
  separate experiment below instead of being forced through this filter.
  Eligibility in the report applies the same filter *and* checks holdout
  feasibility (>= 10 test rows, >= 2 training groups).
* **Per-platform** target only (`youtube`/`allstar`: views, `instagram`: likes —
  documented proxy, `reddit`/`tiktok`: none, so they cannot train). No pooled
  cross-platform raw metric.
* The scored set is **verified-only** by default; `training.allow_unverified_groups`
  opts into scoring all rows and labels the residual risk `unverified_risk`.
  There is no blanket coverage-ratio gate to game.
* Target = `log1p(metric) − cohort median`, with the cohort medians fitted on
  **training rows only** (cohort needs >= 3 training rows). A held-out row in an
  unseen/thin cohort uses the training-wide median and is counted as a fallback.
  It is the *observed cumulative* count, never a fabricated 24h figure, and never
  a causal claim.
* Pre-publication features only. Any outcome-named feature (`views`, `likes`,
  `comments`, `shares`, `saves`, `followers`, …) or measurement-time feature
  (`age_hours_at_observation`) raises `ValueError` before a matrix exists.
* Chronological 80/20 split, then every training row whose identity group also
  appears in the test block is purged. Groups are **connected components** over
  verified clip *and* match identities, so two clips of the same match stay in
  one group. Ambiguous/fuzzy links and unlinked posts get a private `post_only`
  group and are never called verified.
* The scored set is **restricted to verified identities** (unambiguous,
  confidence >= 0.8). Rows with an unverified identity are excluded and counted;
  there is no coverage-ratio threshold, so no leakage-free claim rests on a
  percentage. `training.allow_unverified_groups: true` opts into scoring all rows
  and labels the residual risk `unverified_risk`. Deciding factors are reported
  per platform.
* Baselines are scale-correct: on the cohort-relative target the cohort median
  predicts 0.0 by construction (labelled), and on the absolute `log1p` Allstar
  scale an unseen creator falls back to the **training-wide median** — never 0.0,
  which would mean "zero exposure". A baseline is omitted when the creator
  feature is absent from training or held-out rows.
* Models: cohort-median and creator-median baselines, Ridge (train-only scaling),
  HistGradientBoosting when scikit-learn is installed. Ablations strictly follow
  feature kinds: `all`, `no_gameplay`, `no_presentation` (drops
  `format_label`/duration), `no_context` (drops `creator_name`/caption/time).
  Metrics: MAE, average-rank Spearman (tie-safe), deterministic tie-aware
  precision@k.
* Fewer than 10 test rows or fewer than 2 training groups → refusal, no score.
* Zero engagement is kept (target `log1p`, declared). Unknown feature values get
  explicit `<feature>__missing` indicators. `age_hours_at_observation` is stored
  for audit only and is rejected as a predictor; a residual-vs-age diagnostic is
  reported with `ridge:all`.

### Separate `allstar_gameplay` experiment

Allstar clips have no publication dates, so they cannot use the chronological
sweep above. They get their **own** labelled experiment instead
(`evaluate_allstar_gameplay`):

* label = Allstar views at import time, `log1p`, `publication_dates: false`,
  `forecasts_youtube: false` — no date-based validation is claimed;
* groups are **matches** (connected clip+match components); the split is a
  deterministic hash of the group id, so no match straddles train/test;
* ablations: `all`, `no_headshots`, `no_weapons`, `density_only`;
* this is the only place the `format_label = clip` population is scored, and it
  is never pooled with YouTube/Instagram targets.

## Retry policy

Every request is attempted **at most 3 times**, each failure logged with its
attempt number. Exhaustion logs a terminal error and the platform run ends
`failed` (the CLI exits non-zero). Rate limiting is a minimum interval plus a
per-run request ceiling; hitting the ceiling ends the run `partial` **with the
posts already fetched** and marks `budget_exhausted` in the run notes.

## Tests

`tests/test_social_sweep_{store,platforms,features_identity,train,cli,deepen,refresh,backfill,linkage,analytics,own_backfill,dossier,dossier_build}.py`
(160+ tests) — idempotent ingestion, immutable snapshots, NULL metrics, retry cap,
credential non-persistence, marker-validated format evidence, dormant-channel
labelling, per-creator caps, Instagram bad-handle resilience, discovery recency
provenance, hypothesis links cleared/ambiguous, exact-only gameplay attachment,
match-held-out Allstar experiment, `log1p` on zero engagement, missing-value
indicators, tie-safe ranking, refresh append-only intervals, a repeat-safe CLI collection,
verified-only own-channel linkage rules (single candidate exact, ambiguity
rejected, no-map/ wrong-signature / map-mismatch rejected), linkage idempotency +
purge survival + retry cap, own-channel completeness vs competitor caps, and
analytics separation (own family, windowed, append-only, fail-fast on a dead
credential, never feeding the public views target), and dossier rules (bounds
distinguish inferred window from unknown published trim, clip verification is
never claimed exact, missing state reads unavailable, NaN never becomes zero,
other players' state is never attributed to the POV player, chronology is
window-bounded and tick-ordered, one dossier per moment with all clip evidence
merged, atomic + idempotent writes, failures isolated per moment).
