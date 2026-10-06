# Match listener (HLTV + FACEIT)

`cs2archive/listener/daemon.py` watches completed HLTV results **and** FACEIT
notables, and queues POV renders for either source. It lives in
`cs2archive/listener/` (not `cs2archive/hltv/`) because HLTV is only half of
it; the old `cs2archive/hltv/match_listener.py` path is a deprecated shim.

On the HLTV side it queues matches from the configured event when at least one
team is in the top-20 ranking snapshot.

The default event is BLAST Open Porto 2026. The default poll interval is five
minutes. One card per map/demo is queued from `backlog/<match>/{high,medium}/`
(rating >= 1.0), picked by **raw HLTV rating — by decision, not by
omission** (F1 DOCFIX: the weight machinery in `score_cards.py` exists and
is inspected manually, but the live queue is rating-ordered; wiring weight
in is recorded as an explicit open decision — flip only on a wave-4
ordering replay showing weight-ordered picks beating rating-ordered picks.
See Weighting below). HLTV's
`/results?date=` query is a no-op (the SPA always returns the latest-results
dump), so the listener reads on-page date headlines and keeps **Featured
results** plus **today and yesterday**. Older dated groups on that dump
(group stage, etc.) are ignored.
Matches that already have backlog cards on disk are marked done and not
rendered again. The worker runs
one `scripts/pov/pipeline.py` process at a time, **up to 2 uploads per local
calendar day** (the YouTube long-form slots). When that POV is youtube-ready,
the listener spawns `scripts/upload/upload_pending.py --dir <overlay> --limit 1`
for **that** overlay folder in a new console, then immediately starts the next
render. `--dir` keeps the scan inside the POV's youtube folder so leftover
pending metas are not picked up.
Shorts candidate timelines are extracted inside `create_backlog.py` (Recognised
Pros only, skip with `--no-shorts`; no HLTV demand veto or extraction-time claims).
`render_pending_shorts` ranks the entire pending pool by predicted Allstar views
and caps successful renders at two per Sydney day. See `shorts-picker.md`.
Output is written only when at least one
short is detected: `renders/pov-<demo-stem>_<nick>/shorts/shorts-<slug>/`.

## FACEIT notables (two tracks)

The listener polls FACEIT every cycle (`discover_faceit_tracks`, one scrape,
15-minute cooldown, lookback stretches 1h→24h to cover time blocked inside
renders). Each scrape splits into:

- **Solo POVs** → the pipeline, sharing the 2/day cap (whatever the
  quality gate passes per scrape, `n=room`). A POV qualifies on
  personal merit: **demand-index star** (>= 1.40, any line — a loss still
  qualifies, but only with sample evidence behind the entry: `demand_star_supported`
  requires recent videos or a deep track record, so stale long-window-only
  thin samples don't auto-pass). There is no org-rank fallback: a
  top-10-org starter with no measurable audience demand never renders
  solo. A mediocre line that only looks
  interesting because the lobby is stacked never renders solo.
- **Highlight lobbies** → `backlog/faceit-highlights/` (record-only feed
  for the WIP highlight path: demo download + lobby record, no render, no
  daily slots). A lobby qualifies on lobby stardom: ≥ 2 Recognised Pros
  with combined org-rank bonus ≥ 800k (two top-5 starters' worth) — no
  line requirement, so all-mid-line barnburners between stacked teams
  still land. One entry per match; a lobby can feed both tracks.

Extra Recognised Pros add a costar chip; K/D scales star bonus within
[0.5, 2.0] (one frags line must not dominate the weight; a disaster map
still pays half). Neither is a gate. A nocries-level demand line qualifies
for neither track. Solo POVs queue whatever the quality gate passes,
up to the remaining daily room — no FACEIT-specific per-day cap, so a
day with several standouts ships several POVs and a quiet day ships
none. Repeats are suppressed by the persisted `used` performance ids
(plus dup-of-uploaded checks), and `room` shrinks as the queue fills,
so scrapes cannot append unbounded picks. A pipeline-completed card
is never re-run (a second run double-appends `completed` and burns a
slot); its dead upload console is respawned only after a
30-minute dead margin (`faceit_upload_spawns` ledger, capped 3 spawns per
card per day — a live console racing a respawn would double-upload the
same video, `upload_pending.py` has no single-instance lock). Demos are downloaded and a single-POV backlog card is
built, then
the same pipeline + upload-spawn path as HLTV cards. Weak leftover games
are not used to pad the day to 2. There is no separate Windows 09:00 task.

## Weighting

`score_cards.py` scores HLTV backlog cards on the same chip scale as
FACEIT notable scoring. Stars come from YouTube, not from
HLTV ranking alone:

| Chip | Source | Cap |
|---|---|---|
| `match_team` | Both teams' highlight-channel demand (BLAST / ESL / PGL / StarLadder / EWC) | 400k |
| `match_highlight` | This fixture's highlight views in the last 7 days | 200k |
| `star` | POV player's org rank / 2, scaled by K/D clamped to [0.5, 2.0] | 400k |
| `demand` | max(POV-channel player index, highlight-named player index) | 200k |
| `rating` | HLTV Rating 3.0 above 1.00 | 160k |

Two of these are now wired into the HLTV queue (the CLI
`score_cards backlog/<match_slug>` still prints the full chip table for
inspection):

- **Gate** — before queueing, `_star_gate_cards` drops every card whose POV
  player fails the FACEIT rule (`_star_eligible` → `scoring.demand_eligibility`
  at `FACEIT_STAR_FLOOR` 1.40: demand star **with sample evidence**, or a
  breakout). Fail-closed: a missing/stale payload drops the match rather than
  widening to rating alone, and the drop is logged per match. `--no-star-gate`
  queues on HLTV rating only.
- **Order** — actionable matches are actioned by `_match_demand_points`
  (both teams' demand via `_resolve_indexed_team`, which tolerates the event
  words HLTV glues onto team 2, plus `match_highlight` views), highest first, so
  the day's scarce slots go to the biggest fixture instead of /results page
  order. `--results-order` restores page order.

Selection is **one card per match** (`select_best_card`): a two-map series used
rating-per-map selection before, and a single series spent both daily slots
(9z vs NAVI on 2026-10-06) while spirit vs mouz and vitality vs falcons waited.

Ordering *within* a match, and any move beyond demand chipping, stays an open
operator decision (F1 DOCFIX). Flip to a pure weight order only on a
rating-blind wave-4 ordering replay with pre-registered bars
(win-rate and margin over rating-ordered picks on held-out performance,
plus renders holding the forward kill metric) — a yardstick that embeds
the graded variable does not count. Refresh:

```powershell
python -m cs2archive.hltv.refresh_stars               # scrape + rewrite both indexes
python -m cs2archive.hltv.refresh_stars --install-cron --at 12:00
python -m cs2archive.hltv.score_cards backlog/<match_slug>
```

The daily Windows task `CS2ArchiveStarRefresh` runs at 12:00 local time and
scrapes competitor POV channels plus `@cs2povarchive` (player stars) and the
official highlight channels (team stars). It is the sole refresher: the
listener performs no inline demand refresh (FACEIT discovery passes
`skip_youtube_demand=True` by design — a scrape inside the 300s poll loop
would stall it). Gate readers check the payload once per run and log its
freshness loudly (`scoring.log_demand_payload_status`, called from
`discover_faceit_tracks`); a stale or version-mismatched payload narrows
the star arm instead of silently widening it to the research table.
`--dry-run` does not scrape YouTube.

## Grading the gate (replay_gate + ledger + forward metrics)

`cs2archive/faceit/replay_gate.py` replays gate configs over past days:
per-day payload rebuild from the stored scrape (production builders only),
then that day's FACEIT candidates through each config (solo, one per
 player+match). Compares `demand-only` (star without the breakout arm) vs
`breakout-or` (the shipped rule: demand rule OR a 30d ≥30x-channel-median
video). Bands are advisory.

```powershell
python -m cs2archive.faceit.replay_gate --from 2026-09-15 --to 2026-10-01
```

- Views are today's captures aged back per replay day (old absolute bars
  understate; medians are stable). `collect()` retries 3x on API flakes.
- Every live scrape appends to `.data/gate_decisions.jsonl` (one summary
  line + one line per candidate with any demand signal) via
  `cs2archive/faceit/gate_ledger.py` — "gate said no" vs "never played".
- `cs2archive/faceit/gate_forward.py` scores our own uploads against the
  same machinery. Two numbers: `perf_ratio` (vs the player's competitor
  median — comparable across players but punishes our small channel) and
  `own_pi` (vs OUR channel median — the kill metric). Kill rule, read as
  the median over ~10 breakout-qualified renders: `own_pi` ≥ 2.0 keep /
  1.0–2.0 hold / < 1.0 tighten or revert. Unaliased rows never score.
  Forward window opened 2026-10-02 (`.data/gate_forward.json`, baseline
  median own_pi 0.89 over 141 videos); decision due 2026-10-23.
- Demand payload note: `update_player_demand.refresh()` writes a
  `breakouts` section (`{nick: max 30d PI}`: long-form, age ≥ 2d,
  velocity floored at 7d, leave-one-out channel baseline, absolute
  reach of 5,000 views or 300 views/day, threshold 30x channel median);
  `scoring.has_recent_breakout()` reads it (missing section = False, no
  fallback). Payload carries `method.rule_version` (atomic write);
  readers treat a missing/stale version as loudly stale, never as a
  silent fallback. Recompute offline with
  `python cs2archive/faceit/update_player_demand.py --offline`.
- Ledger note: `gate_verdict` mirrors the production star-or-breakout
  rule; the spike arm still rides a separate `breakout_or` flag for
  measurement, with the rule version stamped on every line, plus
  `stream` and `event_id` for post-hoc dedupe (listener polls overlap
  ~5x). Synthetic/test lines never go to the production file.

## Grading the star (eval_stars)

`cs2archive/hltv/eval_stars.py` scores the player star system against scraped
competitor POV performance — the same `exports/pov_market/video_history.csv`
the star is built from, used as held-out ground truth. Target per video is
`performance_index` = views/day over its **own channel's** median views/day.

```powershell
python -m cs2archive.hltv.eval_stars                      # 90-day time split
python -m cs2archive.hltv.eval_stars --sweep-days 45,60,90,120
python -m cs2archive.hltv.eval_stars --mode in-sample      # CIRCULAR: reference only
```

- **Protocol:** fit window = videos published strictly before the cutoff;
  the index is rebuilt *as of that cutoff* through the production helpers
  (`update_player_demand.build_index` + `analyze_pov_market.analyze_rows`), so
  only past performance feeds the star. `fit_index_at` drops future rows
  itself — `in_window` only trims the old side.
- **Read the player-level numbers first.** A star is a per-player constant, so
  video-level spearman mostly measures channel/mix noise. Player-level spearman
  (star vs a player's held-out median performance) is the headline, with a
  permutation p-value because only ~13 players clear the index's sample-size
  floors per split. Video-level `precision@K` (K = daily-selection scale) is the
  secondary read.
- **Baselines in the report:** the unclipped fit-fold median performance index
  (shows what the 1.08–1.80 clip, the 1.35 thin-sample cap and the 30-day blend
  cost) and channel subscribers (should not beat the star).
- **Reference run (2026-09-28, 90-day split):** 2045 fit videos / 13 players;
  1602 held-out videos, 41.9% coverage. Star vs log performance ρ=+0.11 video
  level, ρ=−0.09 at player level (p=0.78, n=12) — i.e. the 1.25 gate is roughly
  a coin flip at this sample size, while player-level ρ swings +0.43…−0.18
  across cutoffs. The scrape's recency skew (few players past the sample floors)
  is the binding constraint, not the score formula.
- **JSON** (gitignored): `exports/pov_market/star_eval.json` — bands, sweep,
  per-player table, caveats.

## Run

From the repository root:

```powershell
.\cs2archive\listener\run_listener.ps1 -DryRun -Once
.\cs2archive\listener\run_listener.ps1
```

Use `--no-rebaseline` when switching events so already-completed matches can
still be actioned (launch normally re-baselines everything currently visible).

**Single owner — never two listeners.** The `.listener/hltv.lock` only makes
the loser crash-loop (exit 1 every 30s). Always stop others first:

```powershell
.\cs2archive\listener\stop_listener.ps1
.\cs2archive\listener\run_listener.ps1
```

`run_listener.ps1` also kills stale listener processes on start (skips
its own PID; matches both `listener.daemon` and the pre-rename
`match_listener` command line). Agents launching via `bg_run` must run the same
check-and-kill (python + wrapper) before start.

The first command checks the filters and state flow without downloading or
rendering. State is stored in `.listener/hltv.json`; a lock file beside it
prevents two listeners from using the same CloakBrowser profile or render
worker.

Every launch runs a queue clean: missing card files are dropped, and
FACEIT cards duplicating a completed YouTube upload (same player + demo +
scoreline, e.g. same game re-scraped under a new room id) are skipped and
their dup files + stale render state deleted. Source cards of completed
uploads are dropped from the queue but their files are kept.

Useful commands:

```powershell
python -m cs2archive.listener.daemon --status
python -m cs2archive.listener.daemon --no-refresh-teams --once --dry-run
```

The top-20 team list is the notable-team gate for completed matches: a match
counts only if one side is on it (`select_matches`). It is refreshed
automatically whenever the cached copy is older than `TEAMS_MAX_AGE` (24h), and
always on a state with no list yet, so a restarted listener picks up ranking
changes without anyone remembering a flag. `--no-refresh-teams` pins the cached
list (use for offline/dry runs or to reproduce an old selection).

## Run at logon

```powershell
.\cs2archive\listener\install_listener_task.ps1
schtasks.exe /Run /TN "CS2Archive Match Listener"
```

The task was named `CS2Archive HLTV Match Listener` before the rename; delete a
stale one with
`schtasks.exe /Delete /TN "CS2Archive HLTV Match Listener" /F` after installing.

The scheduled task starts the persistent process at Windows logon. If a
pipeline fails, its card remains in the queue for the next poll. Uploads run
in a separate console as each POV becomes youtube-ready; the listener keeps
rendering. To upload one finished POV by hand (same command the listener
spawns):

```powershell
python scripts/upload/upload_pending.py --dir youtube/{run_id}_overlay --limit 1
```
