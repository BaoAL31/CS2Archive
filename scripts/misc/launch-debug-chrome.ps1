param(
    [switch]$SeedCookies = $true,
    [int]$Port = 9221
)

$DebugProfile = "$env:USERPROFILE\.chrome-debug"
$MainProfile = "$env:LOCALAPPDATA\Google\Chrome\User Data\Profile 2"
$CdpPort = $Port

function Stop-ChromeInstance {
    $processes = Get-Process -Name "chrome" -ErrorAction SilentlyContinue
    foreach ($p in $processes) {
        $cmdline = (Get-WmiObject -Class Win32_Process -Filter "ProcessId=$($p.Id)").CommandLine
        if ($cmdline -match "chrome-debug") {
            Write-Host "Stopping debug Chrome (PID $($p.Id))..."
            Stop-Process -Id $p.Id -Force
        }
    }
}

function Sync-Cookies {
    if (-not (Test-Path $MainProfile)) {
        Write-Host "Main Chrome profile not found at $MainProfile"
        return
    }
    Write-Host "Seeding debug profile with cookies from main profile..."
    $excludes = @("Cache", "Code Cache", "Service Worker", "GPUCache", "IndexedDB")
    foreach ($item in Get-ChildItem $MainProfile) {
        if ($excludes -contains $item.Name) { continue }
        $defaultDir = Join-Path $DebugProfile "Default"
        $dest = Join-Path -Path $defaultDir -ChildPath $item.Name
        if (Test-Path $dest) {
            Remove-Item -Path $dest -Recurse -Force
        }
        Copy-Item -Path $item.FullName -Destination $dest -Recurse -Force
    }
}

Stop-ChromeInstance

if ($SeedCookies) {
    Sync-Cookies
}

Write-Host "Launching debug Chrome on port $CdpPort..."
$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chromePath)) {
    $chromePath = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
}

Start-Process -FilePath $chromePath -ArgumentList @(
    "--remote-debugging-port=$CdpPort",
    "--user-data-dir=$DebugProfile",
    "--no-first-run",
    "--no-default-browser-check"
) -WindowStyle Normal

Write-Host "Debug Chrome is running. Attach Playwright MCP with:"
Write-Host "  --cdp-endpoint=http://localhost:$CdpPort"
