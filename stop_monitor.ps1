$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$escapedProjectDir = [Regex]::Escape($ProjectDir)
$currentPid = $PID

$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $currentPid -and
    $_.CommandLine -and
    $_.CommandLine -match $escapedProjectDir -and
    (
        $_.CommandLine -match "start_monitor\.bat" -or
        $_.CommandLine -match "run_monitor_forever\.ps1" -or
        $_.CommandLine -match "crawler\.py monitor"
    )
}

if (-not $targets) {
    Write-Host "[stop] No Mercari monitor process found for $ProjectDir"
    exit 0
}

foreach ($process in $targets) {
    Write-Host "[stop] Killing PID $($process.ProcessId): $($process.Name)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "[stop] Done"
