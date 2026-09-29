# HLTV match listener

`scripts/hltv/match_listener.py` watches completed HLTV results and queues POV
renders for matches from the configured event when at least one team is in the
top-20 ranking snapshot.

The default event is BLAST Open Porto 2026. The default poll interval is five
minutes. One card per match is queued from `backlog/<match>/{high,medium}/`
(rating >= 1.0), picked by **weight** (not raw HLTV rating). HLTV's
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
Shorts timelines are extracted inside `create_backlog.py` (Recognised Pros
only, skip with `--no-shorts`; low-demand POVs without a NAVI / Spirit /
Vitality hook are dropped). Output is written only when at least one
short is detected: `renders/pov-<demo-stem>_<nick>/shorts/shorts-<slug>/`.

## FACEIT notables (two tracks)

The listener polls FACEIT every cycle (`discover_faceit_tracks`, one scrape,
15-minute cooldown, lookback stretches 1h→24h to cover time blocked inside
renders). Each scrape splits into:

- **Solo POVs** → the pipeline, sharing the 2/day cap (one quality pick per
  scrape, `n=1`). A POV qualifies on
  personal merit: **demand-index star** (>= 1.40, any line — a loss still
  qualifies, but only with sample evidence behind the entry: `demand_star_supported`
  requires recent videos or a deep track record, so stale long-window-only
  thin samples don't auto-pass) **or** top-10-org starter **with** a standout
  line (1.5 K/D / 30 kills / 100 ADR). A mediocre line that only looks
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
for neither track. Solo POVs are queued one per scrape — the goal is at
least one high-quality upload per day, not maxing the 2/day cap. A day
guard stops the FACEIT scrape once today's pick is queued (`faceit_queued`
non-empty): re-enqueueing the SAME card when its pipeline died
pre-completion, never appending more (the old per-scrape `n=room` batching
grew one day to 14 with zero uploads completing). A pipeline-completed card
is never re-run (a second run double-appends `completed` and burns the
day's second slot); its dead upload console is respawned only after a
30-minute dead margin (`faceit_upload_spawns` ledger, capped 3 spawns per
card per day — a live console racing a respawn would double-upload the
same video, `upload_pending.py` has no single-instance lock). The
highlight track stays alive on served days via an `n=0` lobbies-only
scrape; a purged queued card falls through to the scrape so the day still
gets its pick. Demos are downloaded and a single-POV backlog card is
built, then
the same pipeline + upload-spawn path as HLTV cards. Weak leftover games
are not used to pad the day to 2. There is no separate Windows 09:00 task.

## Weighting

HLTV cards use the same chip scale as FACEIT notable scoring. Stars come from
YouTube, not from HLTV ranking alone:

| Chip | Source | Cap |
|---|---|---|
| `match_team` | Both teams' highlight-channel demand (BLAST / ESL / PGL / StarLadder / EWC) | 400k |
| `match_highlight` | This fixture's highlight views in the last 7 days | 200k |
| `star` | POV player's org rank / 2, scaled by K/D clamped to [0.5, 2.0] | 400k |
| `demand` | max(POV-channel player index, highlight-named player index) | 200k |
| `rating` | HLTV Rating 3.0 above 1.00 | 160k |

A 1.3 donk on Spirit vs FURIA outranks a 1.8 unknown on a low-demand map.
Queue order is weight, then rating. Refresh:

```powershell
python scripts/hltv/refresh_stars.py               # scrape + rewrite both indexes
python scripts/hltv/refresh_stars.py --install-cron --at 12:00
python scripts/hltv/score_cards.py backlog/<match_slug>
```

The daily Windows task `CS2ArchiveStarRefresh` runs at 12:00 local time and
scrapes competitor POV channels plus `@cs2povarchive` (player stars) and the
official highlight channels (team stars). The listener still refreshes if
`.data/team_demand_index.json` is older than 24 hours or
`.data/player_demand_index.json` is older than 7 days. `--dry-run` does not
scrape YouTube.

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
.\scripts\hltv\run_match_listener.ps1 -DryRun -Once
.\scripts\hltv\run_match_listener.ps1
```

Use `--no-rebaseline` when switching events so already-completed matches can
still be actioned (launch normally re-baselines everything currently visible).

**Single owner — never two listeners.** The `.listener/hltv.lock` only makes
the loser crash-loop (exit 1 every 30s). Always stop others first:

```powershell
.\scripts\hltv\stop_match_listener.ps1
.\scripts\hltv\run_match_listener.ps1
```

`run_match_listener.ps1` also kills stale listener processes on start (skips
its own PID). Agents launching via `bg_run` must run the same check-and-kill
(python `match_listener.py` + `run_match_listener.ps1` wrappers) before start.

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
python scripts/hltv/match_listener.py --status
python scripts/hltv/match_listener.py --refresh-teams --once --dry-run
```

The ranking list is captured once and reused after restarts. Use
`--refresh-teams` deliberately to update it.

## Run at logon

```powershell
.\scripts\hltv\install_match_listener_task.ps1
schtasks.exe /Run /TN "CS2Archive HLTV Match Listener"
```

The scheduled task starts the persistent process at Windows logon. If a
pipeline fails, its card remains in the queue for the next poll. Uploads run
in a separate console as each POV becomes youtube-ready; the listener keeps
rendering. To upload one finished POV by hand (same command the listener
spawns):

```powershell
python scripts/upload/upload_pending.py --dir youtube/{run_id}_overlay --limit 1
```
