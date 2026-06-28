$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$ConfigPath = Join-Path $ProjectDir "config.json"
$ProxyHost = "127.0.0.1"
$ProxyPort = 7897
$IntervalMinutes = 15
$DBPath = Join-Path $ProjectDir "mercari.db"
$Profiles = @()

if (Test-Path $ConfigPath) {
    try {
        $config = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
        $monitor = $config.monitor
        if ($monitor) {
            if ($monitor.interval_minutes) { $IntervalMinutes = [int]$monitor.interval_minutes }
            if ($monitor.profiles) {
                foreach ($p in $monitor.profiles) {
                    $email = if ($p.email_to) { [string]$p.email_to } else { "" }
                    $kws = @()
                    if ($p.keywords) {
                        $kws = @($p.keywords | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })
                    } elseif ($p.keyword) {
                        $kws = @([string]$p.keyword)
                    }
                    if ($kws.Count -gt 0) {
                        $Profiles += @{ Name = [string]$p.name; Email = $email; Keywords = $kws }
                    }
                }
            }
            if ($Profiles.Count -eq 0) {
                $kws = @()
                if ($monitor.keywords) { $kws = @($monitor.keywords | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() }) }
                elseif ($monitor.keyword) { $kws = @([string]$monitor.keyword) }
                if ($kws.Count -gt 0) { $Profiles += @{ Name = "default"; Email = ""; Keywords = $kws } }
            }
        }
        if ($config.mercari_proxy) {
            $uri = [Uri]$config.mercari_proxy
            if ($uri.Host) { $ProxyHost = $uri.Host }
            if ($uri.Port -gt 0) { $ProxyPort = $uri.Port }
        }
    } catch { Write-Host "[config] Failed: $_" }
}

if ($Profiles.Count -eq 0) { Write-Host "[ERROR] No keywords in config.json"; exit 1 }

Write-Host "=== Mercari Monitor ==="
foreach ($p in $Profiles) { Write-Host "  $($p.Name) -> $($p.Email): $($p.Keywords -join ', ')" }
Write-Host "Interval: ${IntervalMinutes}min | Proxy: ${ProxyHost}:${ProxyPort}"

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-ProxyPort {
    param([string]$HostName, [int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(2000, $false)
        if ($ok) { $client.EndConnect($async); $client.Close(); return $true }
        $client.Close(); return $false
    } catch { return $false }
}

while ($true) {
    while (-not (Test-ProxyPort -HostName $ProxyHost -Port $ProxyPort)) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Waiting for proxy $ProxyHost`:$ProxyPort ..."
        Start-Sleep -Seconds 10
    }

    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Starting monitors for $($Profiles.Count) profile(s)..."

    $jobs = @()
    foreach ($p in $Profiles) {
        $logDate = Get-Date -Format "yyyyMMdd"
        $logFile = Join-Path $LogDir ("monitor_$($p.Name)_${logDate}.log")
        $argsList = @("$ProjectDir\crawler.py", "monitor", "-k") + $p.Keywords + @(
            "--headless", "--db", $DBPath, "--email", "--health-email", "--interval", "$IntervalMinutes"
        )
        if ($p.Email) { $argsList += @("--email-to", $p.Email) }

        $job = Start-Job -Name "mercari_$($p.Name)" -ScriptBlock {
            param($pyArgs, $logPath, $tag)
            & python $pyArgs 2>&1 |
                ForEach-Object {
                    $line = "[$tag] " + $_.ToString()
                    Write-Host $line
                    try { Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 } catch {}
                }
        } -ArgumentList $argsList, $logFile, $p.Name

        $jobs += @{ Id = $job.Id; Name = $p.Name }
        Write-Host "  Started $($p.Name)"
        Start-Sleep -Seconds 5
    }

    $allOk = $true
    while ($allOk) {
        Start-Sleep -Seconds 15
        foreach ($j in $jobs) {
            $jState = (Get-Job -Id $j.Id -ErrorAction SilentlyContinue).State
            if ($jState -ne "Running") {
                Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $($j.Name) died (state: $jState)"
                $allOk = $false
                break
            }
        }
    }

    foreach ($j in $jobs) { Stop-Job -Id $j.Id -ErrorAction SilentlyContinue; Remove-Job -Id $j.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Restarting in 30s..."
    Start-Sleep -Seconds 30
}
