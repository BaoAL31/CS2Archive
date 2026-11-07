#Requires -Version 5.1
<#
.SYNOPSIS
  Alert when the match listener is dead or genuinely stuck.

.DESCRIPTION
  The listener has died silently twice (2026-10-10 13:13, 2026-10-11 16:08) and
  once crash-looped for six hours (713 hard failures) without a single alert:
  the wrapper's restart loop never terminates, so the failure never reached a
  terminal state, and the only alert channel reports terminal state. This
  watchdog closes both holes by ending *itself* with a non-zero code, which does
  terminate and therefore does alert.

  Progress is judged by real work, not by one log's mtime: during a render the
  listener is blocked inside poll_once (running the pipeline), so it prints
  nothing for an hour while the pipeline log advances and CS2/ffmpeg run. The
  first version watched task-output mtimes only and alerted falsely within 15
  minutes of a healthy render.

  Exit codes: 1 listener process gone, 2 no progress for -StallMinutes,
  3 nothing to watch.

.EXAMPLE
  .\cs2archive\listener\watch_listener.ps1
#>
param(
    [int]$CheckSeconds = 60,
    # The listener polls every 300s; a render legitimately blocks it for ~1h, so
    # this bound only applies while NO render process is alive.
    [int]$StallMinutes = 20
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Transcript = Join-Path $Root ".listener\listener.log"

function Get-ListenerAlive {
    $p = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'cs2archive\.listener' }
    return ($p | Measure-Object).Count -gt 0
}

function Get-WorkInFlight {
    # A render/encode in progress counts as progress, whatever the logs say.
    $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('cs2.exe', 'ffmpeg.exe') -or
        ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'pipeline\.py|render_pov|render_hook|render_util_cams|overlay_pov|concat_rounds')
    }
    return ($p | Measure-Object).Count -gt 0
}

function Get-ProgressStamp {
    $files = @()
    if (Test-Path $Transcript) { $files += (Get-Item $Transcript) }
    # Pipeline logs advance continuously through every render step.
    $files += Get-ChildItem (Join-Path $Root "logs\*.log") -ErrorAction SilentlyContinue
    # The listener's own task output only (its wrapper prints the marker above);
    # never this watchdog's file, which would otherwise time itself out.
    foreach ($f in Get-ChildItem (Join-Path $Root ".pi\tasks\*\*.output") -ErrorAction SilentlyContinue) {
        $head = Get-Content $f.FullName -TotalCount 4 -ErrorAction SilentlyContinue
        if ($head -match '\[watchdog\]') { continue }
        if ($head -match 'wrapper-parent') { $files += $f }
    }
    if (-not $files) { return $null }
    return ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
}

Write-Host ("[watchdog] watching listener; check {0}s, stall {1}min (only while no render runs)" -f $CheckSeconds, $StallMinutes)

$lastStamp = Get-ProgressStamp
$lastChange = Get-Date
# The wrapper sleeps 30s between restarts, so a single miss can be a healthy
# restart in progress: require two consecutive misses before alarming.
$missedChecks = 0

while ($true) {
    Start-Sleep -Seconds $CheckSeconds

    if (-not (Get-ListenerAlive)) {
        $missedChecks++
        if ($missedChecks -lt 2) {
            Write-Host "[watchdog] listener not seen (check $missedChecks/2) - could be a wrapper restart; waiting"
            continue
        }
        Write-Warning "[watchdog] listener process gone for $missedChecks checks - exiting non-zero so this alerts"
        exit 1
    }
    $missedChecks = 0

    if (Get-WorkInFlight) {
        # Rendering: the listener is inside poll_once by design. Not a stall.
        $lastChange = Get-Date
        $lastStamp = Get-ProgressStamp
        continue
    }

    $stamp = Get-ProgressStamp
    if ($null -eq $stamp) {
        Write-Warning "[watchdog] nothing to watch (no transcript, no pipeline logs, no listener output)"
        exit 3
    }
    if ($stamp -ne $lastStamp) {
        $lastStamp = $stamp
        $lastChange = Get-Date
        continue
    }
    $idle = ((Get-Date) - $lastChange).TotalMinutes
    if ($idle -ge $StallMinutes) {
        Write-Warning ("[watchdog] listener alive but idle {0:N0} min with no render in flight (last write {1}) - exiting non-zero so this alerts" -f $idle, $lastStamp)
        exit 2
    }
}
