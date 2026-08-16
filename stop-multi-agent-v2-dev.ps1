[CmdletBinding()]
param(
    [string]$ManifestPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ManifestPath) {
    $ManifestPath = Join-Path $root ".multi-agent-dev\v2\processes.json"
}
$manifestPath = [IO.Path]::GetFullPath($ManifestPath)

function Stop-ProcessTree {
    param([int]$ProcessId)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "No Multi-Agent V2 process manifest was found."
    return
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$stopped = @()
$skipped = @()
foreach ($entry in ($manifest.processes | Sort-Object { [int]$_.pid } -Descending)) {
    $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        continue
    }
    $actualStart = $process.StartTime.ToUniversalTime()
    $expectedStart = [DateTime]::Parse([string]$entry.startTimeUtc).ToUniversalTime()
    if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 2) {
        $skipped += "$($entry.role) PID $($entry.pid) (start time mismatch)"
        continue
    }
    Stop-ProcessTree -ProcessId ([int]$entry.pid)
    $stopped += "$($entry.role) PID $($entry.pid)"
}

$deadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    $remaining = @($manifest.processes | Where-Object {
        Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue
    })
    if ($remaining.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)

if ($remaining.Count -gt 0 -or $skipped.Count -gt 0) {
    Write-Warning "Some processes were not stopped safely."
    $skipped | ForEach-Object { Write-Warning $_ }
    $remaining | ForEach-Object { Write-Warning "$($_.role) PID $($_.pid) is still running" }
    exit 1
}

Remove-Item -LiteralPath $manifestPath
Write-Host "Stopped Multi-Agent V2 services:" -ForegroundColor Green
$stopped | ForEach-Object { Write-Host "  $_" }
