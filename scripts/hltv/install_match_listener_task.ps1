param(
    [string]$TaskName = "CS2Archive HLTV Match Listener",
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Launcher = Join-Path $Root "scripts\hltv\run_match_listener.ps1"
$TaskCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""

# The listener itself remains alive and polls.  The scheduled task is configured
# to restart it if Windows terminates it or the machine reboots.
schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
schtasks.exe /Create /TN $TaskName /SC ONLOGON /RL HIGHEST `
    /TR $TaskCommand /F | Out-Host

Write-Host "Installed: $TaskName"
Write-Host "Start now: schtasks.exe /Run /TN `"$TaskName`""
Write-Host "Stop:      schtasks.exe /End /TN `"$TaskName`""
