$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"

Set-Location $Root
$env:PYTHONPATH = "$Root\scripts;$Root"
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Transcript = Join-Path $LogDir "stars.log"
Start-Transcript -Path $Transcript -Append | Out-Null
try {
    & $Python (Join-Path $Root "scripts\hltv\refresh_stars.py")
    $Code = $LASTEXITCODE
} finally {
    Stop-Transcript | Out-Null
}
exit $Code
