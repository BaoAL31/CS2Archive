# Shorts social evidence — first bounded collection (2026-11-05)

> Superseded by `shorts-social-evidence-expansion-2026-11-05.md`, which expands
> this store to 2,183 posts / 14 YouTube creators / 501 verified Shorts and adds
> the match-held-out Allstar experiment. Kept for the before/after comparison.

Operational report for `python -m cs2archive.shorts.social_sweep`. Interface and
schema are documented in `docs/agents/social-sweep.md`. This file records what the
**first real bounded run** actually produced in this environment, including the
coverage it does not have.

## Commands run

```powershell
$py = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
& $py -m cs2archive.shorts.social_sweep collect --config sources.json     # exit 0
& $py -m cs2archive.shorts.social_sweep enrich  --config sources.json     # exit 0
& $py -m cs2archive.shorts.social_sweep report  --config sources.json --out data/social-sweep/coverage_report.txt
& $py -m cs2archive.shorts.social_sweep train   --config sources.json --experimental   # exit 0
```

Artifacts (all local, untracked): `data/social-sweep/shorts_evidence.sqlite3`,
`data/social-sweep/raw/`, `data/social-sweep/coverage_report.txt`,
`data/social-sweep/train_report.json`. Config: repo-root `sources.json`.

After the review the database was rebuilt once so every observation carries
marker-validated format evidence and a raw-response backlink. The pre-review
database and its raw responses are preserved under
`data/social-sweep/archive/pre-review/` (snapshots are append-only; nothing was
edited in place). The rebuild re-observed YouTube/Instagram/Reddit at the current
time and re-imported Allstar deterministically from the preserved store.

## Actual platform coverage

| Platform | Access status | Posts | Snapshots | Metrics actually non-null | Errors |
|---|---|---|---|---|---|
| YouTube | supported (Data API v3) | 80 | 80 | views/likes/comments/followers 80/80 | 0 |
| Instagram | partial (Business Discovery) | 25 | 25 | likes/comments/followers 25/25; views/plays 0 | 0 |
| Reddit | partial (public RSS) | 40 | 40 | none (RSS exposes no counts) | 0 |
| Allstar | partial (preserved-store import) | 300 | 300 | views 300/300 | 0 |
| TikTok | **blocked** | 0 | 0 | none | 1 blocked preflight row |

Totals: 445 posts, 445 metric snapshots (all raw-linked), 300 identity links,
9,780 feature rows, 2 quarantines. 295 posts have measured foundation gameplay
(298 exact links, 3 alignments rejected as partial); 145 posts have none.

Collection window: 2026-08-01 → now. YouTube returned 2026-09-03 → 2026-11-04
(80-post cap reached before August — the window is *not* fully covered; raise
`max_posts`/`max_pages` or start from `video_ids` to reach older uploads).

### Real distribution (why this dataset can answer the question)

* YouTube Shorts (`format_label=short`, 200 **plus** a `/shorts/` or
  `isShortsEligible` marker): 23 posts, **5–853 views**, e.g. `j41039mYjEI`
  24 s / 52 views, `l8-UCgas8xE` 853 views, `cV9matdPuzU` 107 views. Long-form
  on the same channel: 57 posts, 3–117 views — **excluded from training** by the
  verified-format filter.
* Instagram: 25 Reels, likes 1–13,536 (one clear outlier), comments 0–12.
* Allstar: 300 clips, views 9–676,514 (deliberately not views-filtered; the two
  known quarantined alignments are excluded from training).
* Reddit: 40 posts from `r/GlobalOffensive` only — the per-platform `max_posts`
  cap was hit inside the first community, so `cs2` and `csgoclips` were not
  queried this run.

Everything above is **cumulative count at observation time**. There is no
24-hour history anywhere in this store, so "views at 24h" cannot be computed and
is not stored or imputed.

## What could not be collected (explicit, not faked)

* **TikTok metrics** — cached token scope is `user.info.basic,video.upload`; the
  Display API needs `video.list`, and the Research API needs an approved
  application. oEmbed returns title/author/thumbnail only, so all counts are NULL.
  To unblock: grant `video.list` to the TikTok app and re-login, or supply an
  authorized account export via `imports`.
* **Allstar live reads** — `prt.allstar.gg/playlist` → `403
  {"message":"Error: AUTHENTICATION FAILED"}` for anonymous clients; `allstar.gg/clip/<id>`
  is an SPA shell. Needs an Allstar partner/API credential. The 300 posts above
  come from the previously preserved probe payloads.
* **Reddit scores/comments** — `/r/<sub>/new.json` returns HTTP 403 to this
  client; RSS is the only public path and carries no counts. An OAuth app
  (client id/secret) would be required for `score`/`num_comments`.
* **Instagram plays/duration** — Business Discovery does not expose them.
* **Allstar publication time** — the preserved payloads never recorded a
  confirmed publication timestamp (`publication_time_known` absent), so
  `published_at` is NULL for all 300 imported clips. That is why Allstar cannot
  train: the chronological holdout needs publication order.
* **Gameplay features for YouTube/Instagram/Reddit** — those posts have no
  verified foundation clip link, so all gameplay rows are `unavailable` with a
  reason. Only Allstar posts could link (300 exact `allstar_clip_id`; 295
  measured after 3 partial alignments were rejected; 2 quarantined).
* **Raw-response linkage** — every snapshot points at the exact raw response
  batch that produced it (YouTube 80, Instagram 25, Reddit 40, Allstar 300). For
  re-imported Allstar observations the link lives in `snapshot_provenance`, so
  the immutable snapshot row is never rewritten.
* **Cross-platform same-gameplay groups** — none this run (0 multi-platform
  groups). The same clip would need to appear on two platforms with an exact
  shared identity; fuzzy player+map+round links are stored as `ambiguous` and
  never grouped.

## Experimental sweep result

`train --experimental` → overall **refused** (exit 1). No model was fit, which is
the correct result for this data:

```
[youtube] refused
  only 23 verified short-format rows (minimum 25); no model was fit
  format rejections: {'format=long': 57}
  blocked: 23 < 25 rows; holdout would be 5 rows (< 10); identity coverage 0% < 80%
[instagram] refused
  identity coverage 0% of rows (0/25) is below 80%: same-gameplay leakage between
  train and test cannot be ruled out
  blocked: holdout would be 5 rows (< 10); identity coverage 0% < 80%
[allstar] refused  0 verified short-format rows (298 rejected as unverified clip)
[reddit] / [tiktok] refused  no comparable exposure metric on those paths
```

The previous report's YouTube scores are withdrawn: they were trained on 57
long-form videos plus 23 Shorts, and they mixed held-out outcomes into cohort
medians. With the verified-short filter and train-only cohort fitting there is
not enough data to score anything honestly. The earlier `hgb` numbers must not be
cited anywhere.

What would unlock a fit: >= 25 verified Shorts with >= 10 in the chronological
holdout, unique/verified identity groups, and (for gameplay) posts that link to
staged foundation clips. At present YouTube has 23 verified Shorts, Instagram has
25 Reels (5 holdout rows, zero clip identity), and Allstar has 300 measured clips
but no verified publication time.

## On the makazze Short (52 views)

The store now holds the surrounding cohort: 23 channel Shorts from 2026-09-03 →
2026-11-04 with 5–853 views, plus 57 same-channel long-form videos (3–117, now
excluded from any model). The 52-view Short sits in the middle of recent Shorts
and **below** the 853/386-view Shorts from early September. Publication date,
caption/hashtags and duration are recorded, but no gameplay feature is measured
for it (no foundation link), so this database cannot attribute its view count to
gameplay. The low recent numbers remain confounded by time/cohort (and the
August 1–2k uploads are outside this capped pull). The dataset is now able to
show that confound; it cannot yet separate gameplay from it, and the report says
so explicitly.

## Left untouched

Production `allstar_selector.py`, the picker, rendering, uploads, demos and
avatars were not modified. `AGENTS.md` was not edited to avoid colliding with
unrelated in-flight changes in that file — a one-row link to
`docs/agents/social-sweep.md` should be added when it next lands.
