[CmdletBinding()]
param(
    [int]$BackendPort = 8016,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $root ".tmp\multi-agent-v3-runtime"
$manifestPath = Join-Path $runtimeRoot "services.json"
$manifestPorts = @()

function Stop-ProcessTree([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $taskkill = Start-Process `
        -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
        -ArgumentList @('/PID', "$ProcessId", '/T', '/F') `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($taskkill.ExitCode -ne 0 -or (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Get-ListeningProcessIds([int]$Port) {
    $lines = @(netstat -ano -p tcp | Select-String -Pattern ("\s+\S+:$Port\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$"))
    foreach ($line in $lines) {
        [int]$line.Matches[0].Groups['pid'].Value
    }
}

if (Test-Path -LiteralPath $manifestPath) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $manifestPorts = @($manifest.services | ForEach-Object { [int]$_.port })
        foreach ($entry in @($manifest.services)) {
            $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
            if (-not $process) {
                continue
            }
            $actualStart = $process.StartTime.ToUniversalTime()
            $expectedStart = [DateTimeOffset]::Parse(
                [string]$entry.startTimeUtc,
                [Globalization.CultureInfo]::InvariantCulture
            ).UtcDateTime
            if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 2) {
                Stop-ProcessTree -ProcessId ([int]$entry.pid)
            }
        }
    } finally {
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in (@($BackendPort, $FrontendPort) + $manifestPorts | Sort-Object -Unique)) {
    foreach ($listenerPid in @(Get-ListeningProcessIds -Port $port)) {
        Stop-ProcessTree -ProcessId $listenerPid
    }
}

Write-Host "Stopped V3 services on ports $BackendPort and $FrontendPort."
