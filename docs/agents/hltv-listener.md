# HLTV match listener

`scripts/hltv/match_listener.py` watches completed HLTV results and queues POV
renders for matches from the configured event when at least one team is in the
top-20 ranking snapshot.

The default event is BLAST Open Porto 2026. The default poll interval is five
minutes. One card per match is queued from `backlog/<match>/{high,medium}/`
(rating >= 1.0), picked by **weight** (not raw HLTV rating). The worker runs
one `scripts/pov/pipeline.py` process at a time, **up to 3 uploads per local
calendar day** (the YouTube long-form slots). When that POV is youtube-ready,
the listener spawns `scripts/upload/upload_pending.py --dir <overlay> --limit 1`
for **that** overlay folder in a new console, then immediately starts the next
render. `--dir` keeps the scan inside the POV's youtube folder so leftover
pending metas are not picked up.
Shorts timelines are extracted inside `create_backlog.py` (Recognised Pros
only, skip with `--no-shorts`; low-demand POVs without a NAVI / Spirit /
Vitality hook are dropped). Output is written only when at least one
short is detected: `renders/shorts/shorts-<demo-stem>/shorts-<slug>/`.

## FACEIT notables (off days)

The listener also reads upcoming / live matches from the event page (and the
event matches tab when the overview has no timestamps). If nothing is live
and nothing starts in the next 24 hours, it **keeps polling FACEIT** (same
loop as HLTV, 15-minute scrape cooldown) for watchable Recognised-Pro POVs
from the last 24 hours. A POV qualifies only if it is a **plus-K/D win**
from an HLTV **top-10 org** (donk / kyousuke / m0NESY tier). High K/D or
ADR against a weaker lobby does not qualify. Those are queued as they appear, one
player/match, up to the remaining daily slots. Weak leftover games are
not used to pad the day to 3.
Demos are downloaded and a single-POV backlog card is built, then the same
pipeline + upload-spawn path as HLTV cards.

HLTV leftover work (queued or still-discovered matches) is finished before
FACEIT filler. FACEIT is not mixed into a tournament day just to use empty
slots. There is no separate Windows 09:00 task.

## Weighting

HLTV cards use the same chip scale as FACEIT notable scoring. Stars come from
YouTube, not from HLTV ranking alone:

| Chip | Source | Cap |
|---|---|---|
| `match_team` | Both teams' highlight-channel demand (BLAST / ESL / PGL / StarLadder / EWC) | 400k |
| `match_highlight` | This fixture's highlight views in the last 7 days | 200k |
| `star` | POV player's org rank / 2 (K/D >= 1) | 200k |
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

## Run

From the repository root:

```powershell
.\scripts\hltv\run_match_listener.ps1 -DryRun -Once
.\scripts\hltv\run_match_listener.ps1
```

Use `--no-rebaseline` when switching events so already-completed matches can
still be actioned (launch normally re-baselines everything currently visible).

The first command checks the filters and state flow without downloading or
rendering. State is stored in `.listener/hltv.json`; a lock file beside it
prevents two listeners from using the same CloakBrowser profile or render
worker.

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
