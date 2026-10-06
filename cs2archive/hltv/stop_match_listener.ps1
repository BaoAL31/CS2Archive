#Requires -Version 5.1
<#
.SYNOPSIS
  DEPRECATED forwarder -> cs2archive\listener\stop_listener.ps1
.DESCRIPTION
  The listener moved out of hltv/ (it polls FACEIT too). This forwarder keeps
  stale command lines and muscle memory working.
#>
param()

Write-Warning "DEPRECATED cs2archive\hltv\stop_match_listener.ps1 -> cs2archive\listener\stop_listener.ps1"

& (Join-Path $PSScriptRoot "..\listener\stop_listener.ps1")
exit $LASTEXITCODE
