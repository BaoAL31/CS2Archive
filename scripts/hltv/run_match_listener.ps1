param(
    [int]$Interval = 300,
    [switch]$Once,
    [switch]$DryRun,
    [switch]$RefreshTeams
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
$Args = @(
    (Join-Path $Root "scripts\hltv\match_listener.py"),
    "--interval", $Interval
)
if ($Once) { $Args += "--once" }
if ($DryRun) { $Args += "--dry-run" }
if ($RefreshTeams) { $Args += "--refresh-teams" }

Set-Location $Root
$env:PYTHONPATH = "."
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Transcript = Join-Path $LogDir "listener.log"
Start-Transcript -Path $Transcript -Append | Out-Null
try {
    do {
        & $Python @Args
        $Code = $LASTEXITCODE
        if (-not $Once) {
            Write-Warning "Listener exited with code $Code; restarting in 30 seconds."
            Start-Sleep -Seconds 30
        }
    } while (-not $Once)
} finally {
    Stop-Transcript | Out-Null
}
exit $Code
