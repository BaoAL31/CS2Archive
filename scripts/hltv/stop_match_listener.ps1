#Requires -Version 5.1
<#
.SYNOPSIS
  Stop every running HLTV match listener (python + wrapper).
.DESCRIPTION
  Single-owner guard companion to run_match_listener.ps1. Kills all
  python.exe processes running match_listener.py and all other powershell
  wrappers running run_match_listener.ps1. Run this before starting a new
  listener so two never compete for .listener/hltv.lock.
.EXAMPLE
  .\scripts\hltv\stop_match_listener.ps1
#>
param()

$ErrorActionPreference = "SilentlyContinue"
$MyPid = $PID
$killed = 0

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'match_listener\.py'
} | ForEach-Object {
    Write-Warning ("Killing listener python PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force
    $killed++
}

Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.ProcessId -ne $MyPid -and $_.CommandLine -match 'run_match_listener\.ps1'
} | ForEach-Object {
    Write-Warning ("Killing listener wrapper PID {0}" -f $_.ProcessId)
    Stop-Process -Id $_.ProcessId -Force
    $killed++
}

if ($killed -eq 0) { Write-Host "No listener processes found." }
else { Write-Host "Killed $killed listener process(es). Lock frees on exit." }
