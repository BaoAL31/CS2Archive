# POV investigation and measurement fixes — 2026-10-28

## Finding

There is direct older evidence of restricted exposure, beyond the public-view
comparison in the initial audit. The saved Studio export through September 24
contains 26 September POV uploads (at least five minutes, title includes a
fixture or FACEIT): 476 views, 2,620 thumbnail impressions, median 87 impressions
per upload, and impression-weighted CTR 6.30%. These are low-volume historical
observations, not an October diagnosis or a universal CTR benchmark. They do not
justify replacing the player roster. Source: [Studio export](../../exports/studio/content-2025-09-24_2026-09-24/Table%20data.csv),
[extracted evidence](../../exports/pov_selection_20261028/studio_september_summary.json).

## Reproduced bugs and repairs

1. `gate_forward.own_channel_rows()` asked a competitor-only helper for own rows.
   Its default fresh-data branch was unreachable. It now explicitly includes
   own-channel seeds and chooses the newest capture per video. Competitor
   baselines still exclude our channel. A regression exercises the real default
   `own_video_pi()` call, not just caller-supplied data.
2. Own-channel baseline included unrelated/unrecognised long-form videos and
   uploads outside the rolling window, whereas the graded POV table excluded
   unknown players. Both now use recognised players, ages 7–180 days.
3. Deleted/private upload-playlist entries can omit `videoPublishedAt`. This
   raised `KeyError` and aborted the entire collector. Such placeholders now log
   a skip and pagination continues. A two-page regression checks that valid
   uploads after unavailable entries are retained.
4. The daily player refresher only refreshed seven-day-old-or-younger uploads.
   Older own counts froze while the forward monitor kept increasing their ages.
   It now refreshes our entire 180-day window separately from incremental
   competitor discovery. The regression verifies that old own IDs are not
   skipped in this call.

Fresh public data was fetched through the repaired collector. It completed for
3,876 videos across nine channels; the repaired monitor finds 368 own rows and
scores 203 recognised-player long-form outcomes in its 7–180-day window. This
refresh changes stored public measurements; the live demand payload was not
refitted as part of this investigation. See [post-fix report](../../exports/pov_selection_20261028/forward_after_fix.json).

## Equal-age measurement

Added `python -m cs2archive.upload.export_pov_analytics --days 60 --diagnostics`.
It measures seven complete Pacific calendar days following publication day,
with a three-day reporting buffer. It exports views, watch time, average view
duration/percentage and subscribers, optionally traffic-source reports and
retention curves. It validates the authenticated channel against our history,
dedupes input captures, does not turn unavailable data into zero views, and caps
retryable HTTP failures at three attempts. It writes an atomic JSON result and
never changes YouTube uploads.

The calendar and report definitions follow Google's [day dimension](https://developers.google.com/youtube/analytics/dimensions#day)
and [channel reports](https://developers.google.com/youtube/analytics/channel_reports).
The buffer is conservative scheduling, not a guarantee that estimates are final.
Thumbnail impressions/CTR still require Studio; this exporter does not pretend
annotation/card/ad impression metrics are thumbnail impressions.

## Verified existing repair

The current working tree already contains upload-receipt preservation on a step-6
rerun and an exclusive upload lock, addressing the known sh1ro duplicate.
Their eight regression tests pass. Those changes predate this investigation and
were preserved; public duplicate listings were not deleted or hidden.

## Remaining access requirement and selection scope

Analytics refresh fails with `invalid_grant`. Browser access also stopped because
Computer Use could not verify the Windows browser URL. The user needs to run
`python -m cs2archive.upload.auth_youtube_analytics` and complete Google consent.
The exporter then supplies current equal-age outcomes and traffic/retention data.
Its request shape, channel guard and reporting-window logic are tested; live
Analytics execution remains unverified until login is restored.

No evidence-based change to the live ranking or a specific performance floor
could be established from the available data. The player gate, rating order,
queue, uploads and listener process were left unchanged. Several matching games
succeeded on competitor channels, so a player switch is not a demonstrated fix
for the audience gap. Current queue weak-tail examples remain review candidates;
they are not declared failed experiments merely from their ratings.

Validation: focused demand, monitor, collector, Analytics and existing duplicate
repair tests; Ruff 0.16.9 on changed Python files; waiver checker and whitespace
check. See the session result for the final test count.
