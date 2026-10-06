#Requires -Version 5.1
<#
.SYNOPSIS
  DEPRECATED forwarder -> cs2archive\listener\install_listener_task.ps1
.DESCRIPTION
  The listener moved out of hltv/ (it polls FACEIT too), and the task was renamed
  from "CS2Archive HLTV Match Listener" to "CS2Archive Match Listener". Running
  this forwarder installs the NEW name; delete the stale old-name task with
  schtasks.exe /Delete /TN "CS2Archive HLTV Match Listener" /F
#>
param(
    [switch]$KeepLegacyTaskName
)

Write-Warning "DEPRECATED cs2archive\hltv\install_match_listener_task.ps1 -> cs2archive\listener\install_listener_task.ps1"

$taskName = if ($KeepLegacyTaskName) { "CS2Archive HLTV Match Listener" } else { "CS2Archive Match Listener" }
& (Join-Path $PSScriptRoot "..\listener\install_listener_task.ps1") -TaskName $taskName
exit $LASTEXITCODE
