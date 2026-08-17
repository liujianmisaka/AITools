[CmdletBinding()]
param(
    [string]$PublicHost = "127.0.0.1",
    [int]$CorePort = 8011,
    [int]$WebPort = 8021,
    [int]$InternalPort = 8022,
    [int]$FrontendPort = 5174,
    [string]$WorkspaceId = "aitools",
    [string]$WorkspaceConfigPath = "",
    [string]$ComposeProjectName = "multi-agent-v2-dev",
    [string]$InfrastructureSecretsPath = "",
    [int]$HeartbeatSeconds = 30,
    [switch]$Detached,
    [switch]$SkipFrontendInstall,
    [switch]$SkipInfrastructure,
    [switch]$KeepInfrastructure
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runRoot = Join-Path $root ".multi-agent-dev\v2"
$logRoot = Join-Path $runRoot "logs"
$manifestPath = Join-Path $runRoot "processes.json"
$coreProject = Join-Path $root "multi-agent-v2"
$webProject = Join-Path $root "multi-agent-web-v2"
$frontend = Join-Path $webProject "frontend"
$composeFile = Join-Path $coreProject "deploy\local\compose.yaml"

function Test-ListeningPort {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        return
    }
    $treeKill = Start-Process `
        -FilePath (Join-Path $env:SystemRoot "System32\taskkill.exe") `
        -ArgumentList @("/PID", "$ProcessId", "/T", "/F") `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($treeKill.ExitCode -ne 0) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
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

function Assert-ManagedProcessesRunning {
    param([object[]]$Entries)
    $dead = @($Entries | Where-Object {
        -not (Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue)
    })
    if ($dead.Count -eq 0) {
        return
    }
    $details = @($dead | ForEach-Object {
        $tail = Get-Content -LiteralPath ([string]$_.stderr) -Tail 30 -ErrorAction SilentlyContinue
        "$($_.role) exited during startup:`n$($tail -join [Environment]::NewLine)"
    })
    throw ($details -join [Environment]::NewLine)
}

function Invoke-CheckedCommand {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

function New-HexSecret {
    return [Convert]::ToHexString(
        [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    ).ToLowerInvariant()
}

function Get-InfrastructureSecrets {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        try {
            $document = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        } catch {
            throw "Infrastructure secrets file is not valid JSON: $Path"
        }
        foreach ($name in @("postgresAdminPassword", "temporalDatabasePassword", "controlDatabasePassword")) {
            if (-not $document.$name -or ([string]$document.$name).Length -lt 32) {
                throw "Infrastructure secrets file is missing a valid $name value: $Path"
            }
        }
        return $document
    }

    $document = [ordered]@{
        version = 1
        postgresAdminPassword = New-HexSecret
        temporalDatabasePassword = New-HexSecret
        controlDatabasePassword = New-HexSecret
    }
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $document | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8
        Move-Item -LiteralPath $temporary -Destination $Path
    } finally {
        Remove-Item -LiteralPath $temporary -ErrorAction SilentlyContinue
    }
    return [pscustomobject]$document
}

function Stop-Infrastructure {
    param(
        [string]$ProjectName,
        [string]$FilePath
    )
    Write-Host "Stopping local PostgreSQL and Temporal..."
    Invoke-CheckedCommand -Name "Docker Compose shutdown" -FilePath "docker" -Arguments @(
        "compose", "--project-name", $ProjectName, "-f", $FilePath, "down", "--remove-orphans"
    )
}

function Stop-InfrastructureSafely {
    param(
        [string]$ProjectName,
        [string]$FilePath
    )
    try {
        Stop-Infrastructure -ProjectName $ProjectName -FilePath $FilePath
    } catch {
        Write-Warning "Infrastructure cleanup failed: $($_.Exception.Message)"
    }
}

New-Item -ItemType Directory -Force -Path $runRoot, $logRoot | Out-Null
$env:UV_CACHE_DIR = Join-Path $runRoot "uv-cache"
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
$npmExecutable = (Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue).Source
if (-not $npmExecutable) {
    throw "npm.cmd is required for supervised Windows startup but was not found."
}
if (-not $SkipInfrastructure -and -not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker with Compose is required but was not found."
}
if (-not $SkipInfrastructure) {
    Invoke-CheckedCommand -Name "Docker Compose preflight" -FilePath "docker" -Arguments @(
        "compose", "version"
    )
}

if (-not $WorkspaceConfigPath) {
    $WorkspaceConfigPath = Join-Path $runRoot "workspaces.json"
}
$workspaceConfigAbsolute = [IO.Path]::GetFullPath($WorkspaceConfigPath)
if (-not (Test-Path -LiteralPath $workspaceConfigAbsolute)) {
    $repositoryParent = Split-Path -Parent $root
    $worktreeRoot = Join-Path $repositoryParent ".multi-agent-worktrees\$WorkspaceId"
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
    & $npmExecutable install --prefix $frontend --cache (Join-Path $runRoot "npm-cache")
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
$env:MULTI_AGENT_WEB_V2_DEV_PROXY_TARGET = $publicOrigin

$processes = @()
$infrastructureReady = $false
$infrastructureOwned = $false
try {
    if (-not $SkipInfrastructure) {
        if (-not $InfrastructureSecretsPath) {
            $InfrastructureSecretsPath = Join-Path $runRoot "infrastructure-secrets.json"
        }
        $infrastructureSecretsAbsolute = [IO.Path]::GetFullPath($InfrastructureSecretsPath)
        $secrets = Get-InfrastructureSecrets -Path $infrastructureSecretsAbsolute
        $env:MULTI_AGENT_V2_POSTGRES_ADMIN_PASSWORD = [string]$secrets.postgresAdminPassword
        $env:MULTI_AGENT_V2_TEMPORAL_DB_PASSWORD = [string]$secrets.temporalDatabasePassword
        $env:MULTI_AGENT_V2_CONTROL_DB_PASSWORD = [string]$secrets.controlDatabasePassword
        $env:MULTI_AGENT_V2_DATABASE_URL = (
            "postgresql+asyncpg://multi_agent_app:$($secrets.controlDatabasePassword)" +
            "@127.0.0.1:5432/multi_agent_v2"
        )
        $env:MULTI_AGENT_V2_TEMPORAL_ADDRESS = "127.0.0.1:7233"

        Write-Host "Starting local PostgreSQL and Temporal..."
        $infrastructureOwned = $true
        Invoke-CheckedCommand -Name "Docker Compose startup" -FilePath "docker" -Arguments @(
            "compose", "--project-name", $ComposeProjectName, "-f", $composeFile,
            "up", "-d", "--wait", "--wait-timeout", "120",
            "postgresql", "temporal"
        )
        Invoke-CheckedCommand -Name "Temporal namespace initialization" -FilePath "docker" -Arguments @(
            "compose", "--project-name", $ComposeProjectName, "-f", $composeFile,
            "run", "--rm", "temporal-namespace"
        )
        $infrastructureReady = $true

        Write-Host "Applying database migrations..."
        Invoke-CheckedCommand -Name "Alembic migration" -FilePath "uv" -Arguments @(
            "run", "--project", $coreProject, "alembic",
            "-c", (Join-Path $coreProject "alembic.ini"), "upgrade", "head"
        )
    }

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
    $processes += Start-ManagedProcess -Role "frontend" -FilePath $npmExecutable -Arguments @(
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
        infrastructure = if ($infrastructureReady) {
            [ordered]@{
                composeProjectName = $ComposeProjectName
                composeFile = $composeFile
                secretsFile = $infrastructureSecretsAbsolute
                stopWithServices = -not $KeepInfrastructure
            }
        } else {
            $null
        }
        processes = $processes
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding utf8

    Wait-HttpReady -Name "Control API" -Url "http://127.0.0.1:$CorePort/ready"
    Wait-HttpReady -Name "Web/BFF" -Url "http://${PublicHost}:$WebPort/health"
    Wait-HttpReady -Name "Vite frontend" -Url $viteOrigin
    Wait-HttpReady -Name "Vite Web/BFF proxy" -Url "$viteOrigin/health"
    Assert-ManagedProcessesRunning -Entries $processes

    Write-Host ""
    Write-Host "Multi-Agent V2 development services are ready." -ForegroundColor Green
    Write-Host "Frontend (HMR): $viteOrigin"
    Write-Host "Same-origin Web/BFF: $publicOrigin"
    Write-Host "Control API (loopback): http://127.0.0.1:$CorePort"
    Write-Host "Real user-test workflow: $root\multi-agent-v2\examples\real_user_test\workflow.json"
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
        if ($infrastructureOwned -and -not $KeepInfrastructure) {
            Stop-InfrastructureSafely -ProjectName $ComposeProjectName -FilePath $composeFile
        }
    }
} catch {
    foreach ($entry in ($processes | Sort-Object { [int]$_.pid } -Descending)) {
        Stop-ProcessTree -ProcessId ([int]$entry.pid)
    }
    Remove-Item -LiteralPath $manifestPath -ErrorAction SilentlyContinue
    if ($infrastructureOwned -and -not $KeepInfrastructure) {
        Stop-InfrastructureSafely -ProjectName $ComposeProjectName -FilePath $composeFile
    }
    throw
}
