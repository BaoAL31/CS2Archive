# Short → demo linkage: instrumentation, own-channel backfill, ceiling

Priority pass: every Short must be correlatable with the match demo it came from,
or explicitly recorded as not correlatable. Interface reference:
`docs/agents/social-sweep.md`. No production picker change, no commits, no
uploads/renders, no demo deletion, no scheduled tasks, no paid services.

## Commands run

```powershell
$py = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
& $py -m cs2archive.shorts.social_sweep collect  --config sources.json --platform youtube --no-enrich
& $py -m cs2archive.shorts.social_sweep backfill --config sources.json --build-budget 210 --json
& $py -m cs2archive.shorts.social_sweep analytics --config sources.json [--status]
& $py -m cs2archive.shorts.social_sweep enrich   --config sources.json
& $py -m cs2archive.shorts.social_sweep ceiling  --config sources.json --json
& $py -m cs2archive.shorts.social_sweep report   --config sources.json --out data/social-sweep/coverage_report.txt
& $py -m cs2archive.shorts.social_sweep linkage  --config sources.json
& $py -m cs2archive.shorts.social_sweep train    --config sources.json --experimental
```

## (1) Go-forward instrumentation

| Hook | When | What is written |
|---|---|---|
| `render_shorts._record_linkage` | after the meta step, before the render dir is ever purged | `short_linkage` row (`pending`) + `linkage.json` beside the mp4 + `data/social-sweep/linkage/<short_id>.json` |
| `upload_youtube_shorts._record_linkage` | once a platform post id exists (YouTube, plus Instagram/Facebook ids when present) | row flipped to `linked`, mirror `linkage/<platform>/<post_id>.json`, exact `clip_identity_links` row (`render_upload_linkage`, confidence 1.0) |

Recorded evidence: demo path, match id, map, player steam id + nick, round,
`tick_start`/`tick_end`, kill ticks, `short_type`, `source`
(`allstar_top` / detector), AllStar clip id/views/confidence, selection
provenance, and an evidence hash.

* **Idempotent** — deterministic `short_id` over demo+player+round+ticks+type and
  an `ON CONFLICT` upsert; re-running render or upload changes nothing.
* **Resumable** — `linkage --resolve` re-attempts `pending` rows from the durable
  mirror, which is what makes a purge survivable.
* **Non-fatal** — wrapped in `try_record`: 3 logged attempts (AGENTS.md retry
  cap), then a loud `[linkage][ERROR]`; the render/upload continues.
  `CS2ARCHIVE_NO_LINKAGE=1` disables it entirely.
* **No output change** — the hooks only read the timeline and the post id.

## (2) Own-channel backfill — what the real corpus actually supports

Timeline builds: **202** action timelines built (bounded, ~17 s each) on top of
the cached ones; 574 timeline files exist afterwards. Demos and timelines are
read-only.

| status | rows | meaning |
|---|---|---|
| `pending` | **2** | ledger-proven demo + round, platform post id not recoverable from the ledger |
| `ambiguous` | 2 | two qualifying rounds matched — refused rather than guessed |
| `no_candidate` | 346 | 327 titles carry no kill signature (utility/lineup Shorts), 19 have a signature but no qualifying round in any candidate demo |
| `no_map` | 27 | title names no map — never accepted |

**Exact title-inferred own-channel links: 0.** This is the honest outcome, not a
bug: the local demo corpus is ESL Pro League Season 24 / PGL Bucharest / FACEIT
(255 `.dem`, all Oct 2026), while the own-channel gameplay Shorts reference
EWC 2026, BLAST Open Porto and other events whose demos are not on disk.
`no_candidate` reasons show the verification running over 5,126 verified rounds
and finding no unique signature match for the named map/player.

Two candidate links produced by the first live run were **wrong** and were caught
by review + a new rule:

* `EQcL7m7jssI` — "donk's 4K **against Liquid** on Anubis" matched
  `aurora-vs-spirit-m2-anubis.dem` (Spirit, not Liquid).
* `KolUPYkby9s` — "zont1x … 4K **against G2** on Dust2" matched
  `spirit-vs-m80-m1-dust2.dem` (M80, not G2).

Cause: `RoundFacts.teams` was never populated, so the "opponent (when named)"
restriction in the docs was inert. Fixed — team tokens now come from the fixture
in the folder name (`2398717-falcons-vs-tyloo-…` → `falcons`, `tyloo`), the
opponent phrase parser handles `vs` / `vs.` / `against` and ignores ranked
phrasing (`against 3.1k ELOs`), and a named opponent absent from the demo rejects
the candidate. The three stale pre-fix links were dropped by re-running `enrich`
(which rebuilds derived links). Regression tests cover both wrong cases.

The **ledger path is what proves the instrumentation**: both surviving
`youtube/.shorts_render_selection.json` entries name a demo stem and player in the
render directory (`renders/pov-legacy-vs-1win-m1-dust2_Ax1Le/…`), both demos exist
locally, and both were verified against their timelines and staged `pending` with
the reason "the platform post id is not recorded in the ledger".

## (3) Own-channel completeness

| | before | after |
|---|---|---|
| own posts in the DB | 81 | **375** |
| own verified Shorts | 15 | **153** |
| published range | 2026-09-03 → 11-05 | **2026-05-15 → 11-05** |
| pagination | capped at 2 pages / 60 posts | `own_max_pages: 10`, `full_catalog`, exempt from `per_creator_max` and the window |

YouTube Data API quota used by this tool today: **1,577 units** (own-history pull
+ discovery + probes), reported per run in `report.quota`. Competitor catalogues
stay bounded, so completeness for our channel is not paid for on rivals.

## (4) Analytics layer

`token_youtube_analytics.json` has the right scope but is **expired/revoked**:
`RefreshError: invalid_grant: Token has been expired or revoked.`

* The ingest refuses cleanly, fails fast (one `analytics_auth_failed` row, no
  per-video hammering) and tells you the remediation. Nothing was invented.
* `analytics_snapshots` is empty (`rows=0`); the report now shows
  `own-channel analytics: unavailable` with that reason.
* Analytics remains a **separate family**: `train.load_platform_rows` reads
  `metric_snapshots` only, asserted by a regression test, so impressions/CTR/
  retention can never become or contaminate the public views target.
* To unblock: `python -m cs2archive.upload.auth_youtube_analytics` (browser OAuth
  — operator action, not attempted here).

## (5) Ceiling: what can *ever* be demo-linked

| platform | posts | exact | hypothesis only | title-feasible | evidence-feasible¹ | unlinkable | share |
|---|---|---|---|---|---|---|---|
| youtube | 1,188 | 0 | 189 | 69 | 47 | 952 | 80.1% |
| instagram | 270 | 0 | 0 | 11 | 11 | 212 | 78.5% |
| reddit | 40 | 0 | 0 | 0 | 0 | 40 | 100% |
| allstar | 1,307 | **1,030** | 0 | 150 | 150 | 8 | 0.6% |

¹ upper bound: a local demo for that map exists, but the round signature still has
to verify uniquely.

**Competitor unlinkable share: 80.4% of 1,498 posts** — and that is the ceiling,
i.e. the permanent floor of unlinkable posts is already 80%, because public titles
carry no stable clip id (728 youtube posts name no map at all, 202 carry no
gameplay signature). Fewer than 5% of competitor Shorts could ever be verified
even if every missing demo appeared.

Gameplay-model eligibility is derived from the same exact links the trainer uses:
the only platform with a verified gameplay population remains **Allstar**
(1,030 exact links, 1,024 measured).

## (6) Training / eval after this pass

```
[youtube_own_linked] refused  only 0 own-channel Shorts have a verified demo link
                             (minimum 25); no model was fit
                             analytics: unavailable rows=0 (separate family, never the target)
[allstar_gameplay]   fitted   train=751 test=273 (111/35 matches, match-held-out)
[ youtube | instagram ] refused  0 usable rows with verified_only identity
[ reddit | tiktok ]  refused  no comparable exposure metric on that path
```

`allstar_gameplay` is unchanged by this pass (same 1,030 links):
`baseline_global_median mae=2.2904`, `baseline_creator_median mae=2.7808`
(fallback = training-wide median), `ridge:all mae=2.1812 / spearman=0.3262`,
`ridge:density_only mae=2.1906 / spearman=0.3381`.

## Honest limits

* **Competitor Shorts stay unlinkable**: 80.4% have no evidence path at all. No
  number here claims otherwise.
* **Own-channel title inference produced zero exact links** from the demos on
  disk; the corpus does not contain most of those matches. Only the ledger path
  produced verified records (2), and those await a platform post id.
* **Analytics is blocked on a revoked credential**; coverage is 0 rows.
* `evidence_feasible` counts are upper bounds on the *map* only.
* Extra timelines (202) were built from local demos; no demo was modified.
* The 149 `analytics_query_failed` rows in the DB are from the pre-fix run that
  queried once per video before fail-fast existed; subsequent runs add exactly one
  `analytics_auth_failed` row.

## Left untouched

No production picker/ranking change, no commits, no uploads, no new renders, no
demo or avatar deletion, no OS scheduled task (`refresh_social.ps1` remains
documented and unscheduled per the operator's decision).
