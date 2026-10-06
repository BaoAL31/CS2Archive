#Requires -Version 5.1
<#
.SYNOPSIS
  DEPRECATED forwarder -> cs2archive\listener\run_listener.ps1
.DESCRIPTION
  The listener moved out of hltv/ (it polls FACEIT too). This forwarder keeps
  an already-installed scheduled task or a stale command line working; re-run
  cs2archive\listener\install_listener_task.ps1 to point the task at the new path.
#>
param(
    [int]$Interval = 300,
    [switch]$Once,
    [switch]$DryRun,
    [switch]$RefreshTeams,
    [switch]$NoRefreshTeams,
    [switch]$FaceitHighlights,
    [switch]$NoRebaseline
)

Write-Warning "DEPRECATED cs2archive\hltv\run_match_listener.ps1 -> cs2archive\listener\run_listener.ps1"

$target = Join-Path $PSScriptRoot "..\listener\run_listener.ps1"
& $target -Interval $Interval -Once:$Once -DryRun:$DryRun -RefreshTeams:$RefreshTeams `
    -NoRefreshTeams:$NoRefreshTeams -FaceitHighlights:$FaceitHighlights -NoRebaseline:$NoRebaseline
exit $LASTEXITCODE
