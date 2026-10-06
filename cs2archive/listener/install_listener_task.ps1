#Requires -Version 5.1
<#
.SYNOPSIS
  Install the logon scheduled task for the unified match listener.
.DESCRIPTION
  Registers "CS2Archive Match Listener" (ONLOGON) pointing at
  cs2archive\listener\run_listener.ps1.
.EXAMPLE
  .\cs2archive\listener\install_listener_task.ps1
  schtasks.exe /Run /TN "CS2Archive Match Listener"
#>
param(
    [string]$TaskName = "CS2Archive Match Listener",
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $Root "cs2archive\listener\run_listener.ps1"
$TaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""

# The listener itself remains alive and polls.  The scheduled task is configured
# to restart it if Windows terminates it or the machine reboots.
schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
schtasks.exe /Create /TN $TaskName /SC ONLOGON /RL HIGHEST `
    /TR $TaskCommand /F | Out-Host

Write-Host "Installed: $TaskName"
Write-Host "Start now: schtasks.exe /Run /TN `"$TaskName`""
Write-Host "Stop:      schtasks.exe /End /TN `"$TaskName`""
