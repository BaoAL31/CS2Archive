$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"

Set-Location $Root
# cs2archive is installed (editable), so the repo root alone is enough; the old
# "$Root\scripts" entry and the scripts/hltv/refresh_stars.py path are gone as of CR-01.
$env:PYTHONPATH = "$Root"
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Transcript = Join-Path $LogDir "stars.log"
Start-Transcript -Path $Transcript -Append | Out-Null
try {
    & $Python -m cs2archive.hltv.refresh_stars
    $Code = $LASTEXITCODE
} finally {
    Stop-Transcript | Out-Null
}
exit $Code
