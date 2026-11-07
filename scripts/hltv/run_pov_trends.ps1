#Requires -Version 5.1
# Player-demand refresh only: no HLTV browser harvest, Shorts fitting or renders.
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
Set-Location $Root
$env:PYTHONPATH = "$Root"
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Code = 1
Start-Transcript -Path (Join-Path $LogDir "pov_trends.log") -Append | Out-Null
try {
    & $Python -m cs2archive.faceit.update_player_demand
    $Code = $LASTEXITCODE
} finally {
    Stop-Transcript | Out-Null
}
exit $Code
