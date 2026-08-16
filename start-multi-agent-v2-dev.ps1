[CmdletBinding()]
param(
    [string]$PublicHost = "127.0.0.1",
    [int]$CorePort = 8011,
    [int]$WebPort = 8021,
    [int]$InternalPort = 8022,
    [int]$FrontendPort = 5174,
    [string]$WorkspaceId = "aitools",
    [string]$WorkspaceConfigPath = "",
    [int]$HeartbeatSeconds = 30,
    [switch]$Detached,
    [switch]$SkipFrontendInstall
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runRoot = Join-Path $root ".multi-agent-dev\v2"
$logRoot = Join-Path $runRoot "logs"
$manifestPath = Join-Path $runRoot "processes.json"
$coreProject = Join-Path $root "multi-agent-v2"
$webProject = Join-Path $root "multi-agent-web-v2"
$frontend = Join-Path $webProject "frontend"

function Test-ListeningPort {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Start-ManagedProcess {
    param(
        [string]$Role,
        [string]$FilePath,
        [string[]]$Arguments
    )
    $stdout = Join-Path $logRoot "$Role.out.log"
    $stderr = Join-Path $logRoot "$Role.err.log"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $root `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) {
        $detail = Get-Content -LiteralPath $stderr -Tail 30 -ErrorAction SilentlyContinue
        throw "$Role exited during startup.`n$($detail -join [Environment]::NewLine)"
    }
    return [ordered]@{
        role = $Role
        pid = $process.Id
        startTimeUtc = $process.StartTime.ToUniversalTime().ToString("O")
        executable = $process.Path
        stdout = $stdout
        stderr = $stderr
    }
}

function Wait-HttpReady {
    param(
        [string]$Name,
        [string]$Url,
        [int]$TimeoutSeconds = 45
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 750
        }
    }
    throw "$Name did not become healthy within $TimeoutSeconds seconds: $Url"
}

New-Item -ItemType Directory -Force -Path $runRoot, $logRoot | Out-Null
if (Test-Path -LiteralPath $manifestPath) {
    $existing = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $alive = @($existing.processes | Where-Object {
        Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue
    })
    if ($alive.Count -gt 0) {
        throw "Multi-Agent V2 already has managed processes. Run .\stop-multi-agent-v2-dev.ps1 first."
    }
    Remove-Item -LiteralPath $manifestPath
}

foreach ($port in @($CorePort, $WebPort, $InternalPort, $FrontendPort)) {
    if (Test-ListeningPort -Port $port) {
        throw "Port $port is already in use."
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required but was not found."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is required but was not found."
}

if (-not $WorkspaceConfigPath) {
    $WorkspaceConfigPath = Join-Path $runRoot "workspaces.json"
}
$workspaceConfigAbsolute = [IO.Path]::GetFullPath($WorkspaceConfigPath)
if (-not (Test-Path -LiteralPath $workspaceConfigAbsolute)) {
    $worktreeRoot = Join-Path $runRoot "worktrees"
    New-Item -ItemType Directory -Force -Path $worktreeRoot | Out-Null
    [ordered]@{
        workspaces = @(
            [ordered]@{
                id = $WorkspaceId
                root = $root
                worktreeRoot = $worktreeRoot
                baseRef = "HEAD"
            }
        )
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $workspaceConfigAbsolute -Encoding utf8
}

if (-not $SkipFrontendInstall -and -not (Test-Path -LiteralPath (Join-Path $frontend "node_modules"))) {
    Write-Host "Installing V2 frontend dependencies..."
    & npm install --prefix $frontend --cache (Join-Path $runRoot "npm-cache")
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend dependency installation failed."
    }
}

$internalToken = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
$publicOrigin = "http://${PublicHost}:$WebPort"
$viteOrigin = "http://127.0.0.1:$FrontendPort"
$env:MULTI_AGENT_V2_CONTROL_HOST = "127.0.0.1"
$env:MULTI_AGENT_V2_CONTROL_PORT = "$CorePort"
$env:MULTI_AGENT_V2_ALLOWED_ORIGINS = "[`"$publicOrigin`",`"$viteOrigin`"]"
$env:MULTI_AGENT_V2_WORKSPACE_CONFIG_PATH = $workspaceConfigAbsolute
$env:MULTI_AGENT_WEB_V2_PUBLIC_HOST = $PublicHost
$env:MULTI_AGENT_WEB_V2_PUBLIC_PORT = "$WebPort"
$env:MULTI_AGENT_WEB_V2_INTERNAL_HOST = "127.0.0.1"
$env:MULTI_AGENT_WEB_V2_INTERNAL_PORT = "$InternalPort"
$env:MULTI_AGENT_WEB_V2_CONTROL_API_URL = "http://127.0.0.1:$CorePort"
$env:MULTI_AGENT_WEB_V2_ALLOWED_HOSTS = "[`"$PublicHost`",`"127.0.0.1`",`"localhost`"]"
$env:MULTI_AGENT_WEB_V2_ALLOWED_ORIGINS = "[`"$publicOrigin`",`"$viteOrigin`"]"
$env:MULTI_AGENT_WEB_V2_INTERNAL_STREAM_TOKEN = $internalToken

$processes = @()
try {
    $processes += Start-ManagedProcess -Role "core-api" -FilePath "uv" -Arguments @(
        "run", "--project", $coreProject,
        "uvicorn", "multi_agent_v2.apps.control_api.main:app",
        "--app-dir", (Join-Path $coreProject "src"),
        "--host", "127.0.0.1",
        "--port", "$CorePort",
        "--reload",
        "--reload-dir", (Join-Path $coreProject "src")
    )
    $processes += Start-ManagedProcess -Role "orchestration-worker" -FilePath "uv" -Arguments @(
        "run", "--project", $coreProject, "multi-agent-v2-orchestration-worker"
    )
    $processes += Start-ManagedProcess -Role "agent-worker" -FilePath "uv" -Arguments @(
        "run", "--project", $coreProject, "multi-agent-v2-agent-worker"
    )
    $processes += Start-ManagedProcess -Role "dispatcher" -FilePath "uv" -Arguments @(
        "run", "--project", $coreProject, "multi-agent-v2-dispatcher"
    )
    $processes += Start-ManagedProcess -Role "catalog-refresher" -FilePath "uv" -Arguments @(
        "run", "--project", $coreProject, "multi-agent-v2-catalog-refresher"
    )
    $processes += Start-ManagedProcess -Role "web-bff" -FilePath "uv" -Arguments @(
        "run", "--project", $webProject, "multi-agent-web-v2-dev"
    )
    $processes += Start-ManagedProcess -Role "frontend" -FilePath "npm" -Arguments @(
        "--prefix", $frontend, "run", "dev", "--",
        "--host", "127.0.0.1",
        "--port", "$FrontendPort",
        "--strictPort"
    )

    [ordered]@{
        version = 1
        createdAtUtc = [DateTime]::UtcNow.ToString("O")
        publicUrl = $publicOrigin
        developmentUrl = $viteOrigin
        internalUrl = "http://127.0.0.1:$InternalPort"
        processes = $processes
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8

    Wait-HttpReady -Name "Control API" -Url "http://127.0.0.1:$CorePort/live"
    Wait-HttpReady -Name "Web/BFF" -Url "http://${PublicHost}:$WebPort/health"
    Wait-HttpReady -Name "Vite frontend" -Url $viteOrigin

    Write-Host ""
    Write-Host "Multi-Agent V2 development services are ready." -ForegroundColor Green
    Write-Host "Frontend (HMR): $viteOrigin"
    Write-Host "Same-origin Web/BFF: $publicOrigin"
    Write-Host "Control API (loopback): http://127.0.0.1:$CorePort"
    Write-Host "Logs: $logRoot"

    if ($Detached) {
        return
    }

    Write-Host "Press Ctrl+C to stop all managed V2 processes."
    try {
        while ($true) {
            Start-Sleep -Seconds $HeartbeatSeconds
            $dead = @($processes | Where-Object {
                -not (Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue)
            })
            if ($dead.Count -gt 0) {
                throw "Managed process exited: $($dead.role -join ', ')"
            }
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] V2 services are running."
        }
    } finally {
        foreach ($entry in ($processes | Sort-Object { [int]$_.pid } -Descending)) {
            Stop-ProcessTree -ProcessId ([int]$entry.pid)
        }
        Remove-Item -LiteralPath $manifestPath -ErrorAction SilentlyContinue
    }
} catch {
    foreach ($entry in ($processes | Sort-Object { [int]$_.pid } -Descending)) {
        Stop-ProcessTree -ProcessId ([int]$entry.pid)
    }
    Remove-Item -LiteralPath $manifestPath -ErrorAction SilentlyContinue
    throw
}
