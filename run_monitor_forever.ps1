$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# Edit these values in config.json if you want a different default monitor.
$Keywords = @("WIND BREAKER")
$IntervalMinutes = 15

$ConfigPath = Join-Path $ProjectDir "config.json"
$ProxyHost = "127.0.0.1"
$ProxyPort = 7897

if (Test-Path $ConfigPath) {
    try {
        $config = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
        if ($config.monitor.keywords) {
            $Keywords = @($config.monitor.keywords | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })
        } elseif ($config.monitor.keyword) {
            $Keywords = @([string]$config.monitor.keyword)
        }
        if ($config.monitor.interval_minutes) {
            $IntervalMinutes = [int]$config.monitor.interval_minutes
        }
        if ($config.mercari_proxy) {
            $uri = [Uri]$config.mercari_proxy
            if ($uri.Host) { $ProxyHost = $uri.Host }
            if ($uri.Port -gt 0) { $ProxyPort = $uri.Port }
        }
    } catch {
        Write-Host "[config] Failed to read config.json, using default proxy 127.0.0.1:7897"
    }
}

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-ProxyPort {
    param([string]$HostName, [int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(2000, $false)
        if ($ok) {
            $client.EndConnect($async)
            $client.Close()
            return $true
        }
        $client.Close()
        return $false
    } catch {
        return $false
    }
}

while ($true) {
    while (-not (Test-ProxyPort -HostName $ProxyHost -Port $ProxyPort)) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Waiting for proxy $ProxyHost`:$ProxyPort ..."
        Start-Sleep -Seconds 10
    }

    $logFile = Join-Path $LogDir ("monitor_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Proxy ready. Starting Mercari monitor: $($Keywords -join ', ')"

    & python "$ProjectDir\crawler.py" monitor -k $Keywords --headless --db --email --health-email --interval $IntervalMinutes 2>&1 |
        Tee-Object -FilePath $logFile -Append

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Monitor stopped. Restarting in 30 seconds ..."
    Start-Sleep -Seconds 30
}
