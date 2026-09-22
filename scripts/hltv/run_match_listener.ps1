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
$env:PYTHONPATH = "$Root\scripts;$Root"

# Single-owner guard: kill any other listener before starting. The lock file
# alone only makes the loser crash-loop (exit 1 every 30s); this ensures
# only one owner exists. Skips this wrapper's own PID.
$MyPid = $PID
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'match_listener\.py'
} | ForEach-Object {
    Write-Warning ("Killing stale listener python PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.ProcessId -ne $MyPid -and $_.CommandLine -match 'run_match_listener\.ps1'
} | ForEach-Object {
    Write-Warning ("Killing stale listener wrapper PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$LogDir = Join-Path $Root ".listener"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Transcript = Join-Path $LogDir "listener.log"
Start-Transcript -Path $Transcript -Append | Out-Null
# Culprit log: who started this wrapper (scheduled task = taskeng.exe,
# manual console = explorer.exe/WindowsTerminal, agent = pi/bg runner).
try {
    $me = Get-CimInstance Win32_Process -Filter "ProcessId=$MyPid"
    $par = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $me.ParentProcessId) -ErrorAction SilentlyContinue | Select-Object -First 1
    Write-Host ("[wrapper-parent] pid={0} parent={1} ({2}) cmd={3}" -f $MyPid, $par.Name, $me.ParentProcessId, $par.CommandLine)
} catch { Write-Host ("[wrapper-parent] lookup failed: {0}" -f $_) }
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
