# Recurring metric refresh for the Shorts social-evidence database.
#
# This script only *runs* a bounded, append-only refresh. It does not install a
# daemon or a scheduled task. To schedule it (after reviewing and asking), e.g.:
#
#   schtasks /Create /TN "CS2ArchiveSocialRefresh" /SC DAILY /ST 09:15 ^
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File ^
#          D:\Projects\CS2Archive\cs2archive\shorts\social\refresh_social.ps1"
#
# Manual run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File ^
#     cs2archive\shorts\social\refresh_social.ps1
#
# Every run appends new immutable snapshots for posts older than the threshold;
# no historical snapshot is edited or reconstructed.

param(
    [string]$Config = "sources.json",
    [double]$MinAgeHours = 6,
    [int]$MaxPosts = 300,
    [string]$Python = "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$logDir = Join-Path $repo "data\social-sweep"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "refresh.log"

Push-Location $repo
try {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    "[$stamp] refresh start min_age=${MinAgeHours}h max_posts=$MaxPosts" |
        Out-File -FilePath $log -Append -Encoding utf8
    & $Python -m cs2archive.shorts.social_sweep refresh `
        --config $Config --min-age-hours $MinAgeHours --max-posts $MaxPosts 2>&1 |
        Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    "[$stamp] refresh exit=$code" | Out-File -FilePath $log -Append -Encoding utf8
    exit $code
}
finally {
    Pop-Location
}
