#Requires -Version 5.1
<#
.SYNOPSIS
  Run the unified match listener (HLTV results + FACEIT notables).
.DESCRIPTION
  Single-owner launcher: kills stale listeners, keeps the process alive with a
  30s restart loop, and transcribes everything to .listener/listener.log.
  The listener polls HLTV event results and FACEIT notables every cycle and
  queues whatever is watchable up to the 2/day upload cap.
.EXAMPLE
  .\cs2archive\listener\run_listener.ps1 -DryRun -Once
  .\cs2archive\listener\run_listener.ps1
  .\cs2archive\listener\run_listener.ps1 -EventUrl "https://www.hltv.org/events/8244/esl-pro-league-season-24" -NoRebaseline
#>
param(
    [int]$Interval = 300,
    # HLTV event to track (the notable-team gate is event-scoped). Default is
    # the daemon's own default; pass the currently live event when switching.
    [string]$EventUrl,
    [switch]$Once,
    [switch]$DryRun,
    # Top-20 team list (the notable-team gate) refreshes automatically once the
    # cached copy is older than 24h. -RefreshTeams is kept for compatibility.
    [switch]$RefreshTeams,
    [switch]$NoRefreshTeams,
    [switch]$FaceitHighlights,
    # HLAE hook attempts per render, forwarded to the pipeline/render_pov.
    # The vanilla-viewer flake ("Game error") eats the first launch often
    # enough that the default of 2 is thin; 4 rides it out.
    [int]$HookRetries = 0,
    # Keep the on-disk result baseline AND the on-disk queue. Without this every
    # launch clears state.data["queue"] (fresh edge), which would wipe a
    # manually re-queued POV before it can render.
    [switch]$NoRebaseline
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
$Args = @(
    "-m", "cs2archive.listener.daemon",
    "--interval", $Interval
)
if ($EventUrl) { $Args += @("--event-url", $EventUrl) }
if ($Once) { $Args += "--once" }
if ($DryRun) { $Args += "--dry-run" }
if ($RefreshTeams) { $Args += "--refresh-teams" }
if ($NoRefreshTeams) { $Args += "--no-refresh-teams" }
if ($FaceitHighlights) { $Args += "--faceit-highlights" }
if ($HookRetries -gt 0) { $Args += @("--hook-retries", $HookRetries) }
if ($NoRebaseline) { $Args += "--no-rebaseline" }

Set-Location $Root
# CR-01: cs2archive is an installed package, so the repo root is enough.
$env:PYTHONPATH = "$Root"

# Single-owner guard: kill any other listener before starting. The lock file
# alone only makes the loser crash-loop (exit 1 every 30s); this ensures
# only one owner exists. Skips this wrapper's own PID. Matches both the current
# module path and the pre-rename one so an old console cannot keep the lock.
$MyPid = $PID
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'listener\.daemon|match_listener'
} | ForEach-Object {
    Write-Warning ("Killing stale listener python PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.ProcessId -ne $MyPid -and $_.CommandLine -match 'run_listener\.ps1|run_match_listener\.ps1'
} | ForEach-Object {
    Write-Warning ("Killing stale listener wrapper PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Transcript = Join-Path $LogDir "listener.log"
Start-Transcript -Path $Transcript -Append | Out-Null
# Culprit log: who started this wrapper (scheduled task = taskeng.exe,
# manual console = explorer.exe/WindowsTerminal, agent = pi/bg runner).
try {
    $me = Get-CimInstance Win32_Process -Filter "ProcessId=$MyPid"
    $par = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $me.ParentProcessId) -ErrorAction SilentlyContinue | Select-Object -First 1
    Write-Host ("[wrapper-parent] pid={0} parent={1} ({2}) cmd={3}" -f $MyPid, $par.Name, $me.ParentProcessId, $par.CommandLine)
} catch { Write-Host ("[wrapper-parent] lookup failed: {0}" -f $_) }
$ConsecutiveFailures = 0
# A restart loop that never terminates converts loud failures into silence:
# the ZywOo grand-final render hard-failed 713 times over 6 hours while the
# wrapper absorbed every one, so no task notification ever fired (the channel
# only reports terminal state) and the failures sat unseen in a growing output
# file. Cap the restarts so the failure reaches a terminal state and is alertable.
$MaxRestarts = 3
try {
    do {
        & $Python @Args
        $Code = $LASTEXITCODE
        if ($Code -ne 0) { $ConsecutiveFailures++ } else { $ConsecutiveFailures = 0 }
        if (-not $Once) {
            if ($ConsecutiveFailures -lt $MaxRestarts) {
                Write-Warning ("Listener exited with code {0}; restart {1}/{2} in 30 seconds." -f $Code, $ConsecutiveFailures, $MaxRestarts)
                Start-Sleep -Seconds 30
            } else {
                Write-Warning ("Listener exited with code {0} and failed {1} times in a row - giving up so the failure is visible (a silent restart loop is worse than a stopped listener)." -f $Code, $ConsecutiveFailures)
            }
        }
    } while (-not $Once -and $ConsecutiveFailures -lt $MaxRestarts)
} finally {
    Stop-Transcript | Out-Null
}
exit $Code
