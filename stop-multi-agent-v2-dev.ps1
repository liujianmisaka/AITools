[CmdletBinding()]
param(
    [string]$ManifestPath = "",
    [switch]$KeepInfrastructure
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

if (
    -not $KeepInfrastructure -and
    $manifest.infrastructure -and
    $manifest.infrastructure.stopWithServices
) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Error "Docker is required to stop the managed local infrastructure."
        exit 1
    }
    $secretsFile = [string]$manifest.infrastructure.secretsFile
    if (-not $secretsFile -or -not (Test-Path -LiteralPath $secretsFile)) {
        Write-Error "Infrastructure secrets file is missing: $secretsFile"
        exit 1
    }
    try {
        $secrets = Get-Content -LiteralPath $secretsFile -Raw | ConvertFrom-Json
    } catch {
        Write-Error "Infrastructure secrets file is not valid JSON: $secretsFile"
        exit 1
    }
    $env:MULTI_AGENT_V2_POSTGRES_ADMIN_PASSWORD = [string]$secrets.postgresAdminPassword
    $env:MULTI_AGENT_V2_TEMPORAL_DB_PASSWORD = [string]$secrets.temporalDatabasePassword
    $env:MULTI_AGENT_V2_CONTROL_DB_PASSWORD = [string]$secrets.controlDatabasePassword
    & docker compose `
        --project-name ([string]$manifest.infrastructure.composeProjectName) `
        -f ([string]$manifest.infrastructure.composeFile) `
        down --remove-orphans
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Docker Compose shutdown failed with exit code $LASTEXITCODE."
        exit 1
    }
}

Remove-Item -LiteralPath $manifestPath
Write-Host "Stopped Multi-Agent V2 services:" -ForegroundColor Green
$stopped | ForEach-Object { Write-Host "  $_" }
