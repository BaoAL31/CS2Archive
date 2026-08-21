# HLTV match listener

`scripts/hltv/match_listener.py` watches completed HLTV results and queues POV
renders for matches from the configured event when at least one team is in the
top-20 ranking snapshot.

The default event is Esports World Cup 2026. The default poll interval is five
minutes. Only the highest-rated high-priority card per map is queued from
`backlog/<match>/high/` (rating >= 1.5). The worker runs one
`scripts/pov/pipeline.py` process at a time and does not upload anything.
After a match has demos, it also extracts Shorts timelines with
`scripts/shorts/build_short_timeline.py`. New Shorts output lives under
`renders/shorts/shorts-<demo-stem>/shorts-<slug>/`.

## Run

From the repository root:

```powershell
.\scripts\hltv\run_match_listener.ps1 -DryRun -Once
.\scripts\hltv\run_match_listener.ps1
```

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
pipeline fails, its card remains in the queue for the next poll. After
successful renders, run the normal upload workflow separately:

```powershell
python scripts/upload/upload_pending.py
```
