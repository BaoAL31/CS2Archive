# Early POV trends — implemented 2026-10-28

The user requested that selection follow emerging demand without waiting for
videos to mature. Selection now has a separate early-trend path; the seven-day
Analytics report remains an evaluation tool and never delays this path.

## Live policy

- Public player-demand refresh every two hours, offset to `xx:30`, through
  Windows task `CS2ArchivePOVTrendRefresh`. Launcher:
  `scripts/hltv/run_pov_trends.ps1`; transcript: `.listener/pov_trends.log`.
  Task was run directly and verified with LastTaskResult 0 after implementation.
- Recognised-player long-form POVs may qualify at ages 6 hours–7 days, with
  >=5,000 observed views and >=2x the median total views of at least five older
  observed uploads on their own channel from the last 30 days. Peer observations
  must be at least two days old and at least as old as the candidate observation.
  This deliberately avoids dividing a handful of first-hour views by a tiny age.
- Candidate evidence must have been captured within four hours and automatically
  expires after four hours without refresh (or at publication +7 days).
  Our channel and Shorts are excluded from trend evidence. Missing/malformed
  attribution, publication/capture timestamps or peer evidence do not qualify.
- Durable star and legacy breakout rules remain. The new evidence is stored in
  `early_trends`, independently versioned by `method.early_trend_version: 1`.
  HLTV and FACEIT consult the same predicate. Ledger reasons preserve the source
  video and ratio separately from legacy breakout PI.
- Queue priority is fresh trending players first, rating descending within
  trending and non-trending groups. It updates each poll, including expiry of
  old boosts. The per-series POV is still the highest-rated eligible card;
  full fitted score weights remain diagnostic. Existing HLTV-window precedence,
  one POV per series and two uploads/day remain.
- Recent star-gated matches get one reconsideration per changed demand snapshot
  within the existing today/yesterday result window, using cached cards.
  Already queued/completed successful choices are not rendered twice.

These are initial operating thresholds, not a claim of demonstrated view lift.
Videos failing the early signal are not labelled failures or globally excluded;
the durable gate paths still apply. Late growth can qualify on a later refresh.

## Live verification

After fresh collection, ravie's `nLucy9GlTf0` qualifies for ZywOo at about
39 hours old and 2.3x its older-channel-peer median. Other current early trends
include m0NESY and s1mple. The data path no longer excludes the reference merely
because it is younger than two days.

The current saved queue was reordered under `SingleInstance(.listener/hltv.lock)`
with a full pre-change state backup:
`.listener/hltv.json.bak-trend-priority-20261028-101703-049907`.
ZywOo Anubis vs FUT is now first, ahead of sh1ro Nuke and PR Nuke. No render
process was launched. No listener Python process was running at inspection;
the updated picker code will be loaded at its next normal launch.

A real-collection check exposed an additional timestamp bug: the refresh clock
was sampled before the scrape, causing new captures to look like future data.
The analysis clock is now sampled after collection. A regression reproduces
this at the refresh/writer boundary.

## Analytics access restored

The user completed `python -m cs2archive.upload.auth_youtube_analytics`.
The read-only exporter then successfully fetched metrics, traffic-source data
and retention reports into `exports/pov_market/pov_analytics.json`.

For 59 measurable POVs published within the last 60 days, measured over seven
complete Pacific days after publication day:

- Median views: 10 overall (36 FACEIT: median 9; 23 HLTV: median 10).
- Total views: 661; watch time: 1,515 minutes.
- Traffic includes 346 YouTube search views and 13 related-video views.
  These are view origins, not thumbnail impressions or CTR.
- Only six videos returned retention curves. Missing curves are unavailable,
  not evidence of zero retention. The cohorts are small, and these data do not
  establish a causal reason for recommendation exposure.

The seven-day window excludes the partial publication day. It is an equal-age
comparison, not a measure of final lifetime views or exact first-24-hour views.
No blanket claim about the fraction of lifetime views gained in the first day
was necessary to implement responsive selection.

Validation: 144 focused tests passed (live picker/ledger integration, queue
priority and expiry, stale snapshots, future captures, source parsing, monitoring
and Analytics reporting), Ruff 0.16.9 passed, waiver check passed, changed-file
whitespace checks passed. Existing unrelated working-tree changes were retained.
