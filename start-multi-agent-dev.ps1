[CmdletBinding()]
param(
    [string]$ListenHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$CorePort = 8010,
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8020,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173,
    [string]$WorkspaceId = "aitools",
    [string]$WorkspacePath = $PSScriptRoot,
    [string]$CodexBin = "",
    [string]$CodexHome = "",
    [ValidateRange(5, 120)]
    [int]$StartupTimeoutSeconds = 30,
    [ValidateRange(5, 3600)]
    [int]$HeartbeatSeconds = 30,
    [switch]$Detached
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Assert-PortCanBind {
    param(
        [string]$Address,
        [int]$Port,
        [string]$ServiceName
    )

    try {
        $ipAddress = [Net.IPAddress]::Parse($Address)
    }
    catch {
        throw "开发启动脚本只接受明确的 IP 地址，当前值无效：$Address"
    }

    $listener = [Net.Sockets.TcpListener]::new($ipAddress, $Port)
    try {
        $listener.Start()
    }
    catch {
        throw "$ServiceName 无法绑定 ${Address}:$Port。端口可能已被占用：$($_.Exception.Message)"
    }
    finally {
        $listener.Stop()
    }
}

function Get-ProcessIdentity {
    param([System.Diagnostics.Process]$Process)

    $Process.Refresh()
    $executable = ""
    try {
        $executable = $Process.Path
    }
    catch {
        # Process name and start time remain available for later identity checks.
    }

    return [ordered]@{
        pid = $Process.Id
        process_name = $Process.ProcessName
        started_at_utc = $Process.StartTime.ToUniversalTime().ToString("o")
        executable = $executable
    }
}

function Stop-NewProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    if ($null -eq (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
        return
    }

    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    & $taskkill /PID $Process.Id /T /F *> $null
}

function Wait-ServiceHealth {
    param(
        [string]$ServiceName,
        [string]$HealthUrl,
        [System.Diagnostics.Process]$Process,
        [string]$ErrorLog,
        [int]$TimeoutSeconds
    )

    Write-Host "等待 $ServiceName 健康检查：$HealthUrl" -ForegroundColor Cyan
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastProgress = [DateTime]::UtcNow

    while ([DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            $details = ""
            if (Test-Path -LiteralPath $ErrorLog) {
                $details = (Get-Content -LiteralPath $ErrorLog -Tail 30) -join [Environment]::NewLine
            }
            throw "$ServiceName 进程已退出。$([Environment]::NewLine)$details"
        }

        try {
            $health = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 1
            if ($health.status -eq "ok") {
                Write-Host "$ServiceName 已就绪。" -ForegroundColor Green
                return
            }
        }
        catch {
            # The service is still starting or its reload supervisor is switching workers.
        }

        if (([DateTime]::UtcNow - $lastProgress).TotalSeconds -ge 5) {
            Write-Host "$ServiceName 仍在启动，请稍候……"
            $lastProgress = [DateTime]::UtcNow
        }
        Start-Sleep -Milliseconds 300
    }

    $details = ""
    if (Test-Path -LiteralPath $ErrorLog) {
        $details = (Get-Content -LiteralPath $ErrorLog -Tail 30) -join [Environment]::NewLine
    }
    throw "$ServiceName 在 $TimeoutSeconds 秒内未通过健康检查。$([Environment]::NewLine)$details"
}

function Show-RecentLog {
    param(
        [string]$ServiceName,
        [string]$ErrorLog
    )

    Write-Host ""
    Write-Host "$ServiceName 最近日志：" -ForegroundColor Yellow
    if (Test-Path -LiteralPath $ErrorLog) {
        Get-Content -LiteralPath $ErrorLog -Tail 30 | ForEach-Object {
            Write-Host "  $_"
        }
    }
    else {
        Write-Host "  尚未生成日志。"
    }
}

$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$coreAppDir = Join-Path $root "multi-agent"
$coreReloadDir = Join-Path $coreAppDir "multi_agent"
$webAppDir = Join-Path $root "multi-agent-web"
$webReloadDir = Join-Path $webAppDir "multi_agent_web"
$frontendDir = Join-Path $webAppDir "frontend"
$frontendModules = Join-Path $frontendDir "node_modules"
$runtimeDir = Join-Path $root ".multi-agent-dev"
$manifestPath = Join-Path $runtimeDir "processes.json"
$stopScript = Join-Path $root "stop-multi-agent-dev.ps1"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "未找到共享虚拟环境：$python"
}
if (-not (Test-Path -LiteralPath $coreReloadDir -PathType Container)) {
    throw "未找到核心服务目录：$coreReloadDir"
}
if (-not (Test-Path -LiteralPath $webReloadDir -PathType Container)) {
    throw "未找到 Web 服务目录：$webReloadDir"
}
if (-not (Test-Path -LiteralPath $frontendModules -PathType Container)) {
    throw "未找到前端依赖：$frontendModules。请先在 multi-agent-web\frontend 执行 npm install。"
}
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    throw "未找到 npm.cmd，请确认 Node.js 已通过 Scoop 或其他方式安装。"
}
if (-not (Test-Path -LiteralPath $stopScript -PathType Leaf)) {
    throw "未找到停止脚本：$stopScript"
}
if (@($CorePort, $WebPort, $FrontendPort) | Group-Object | Where-Object Count -gt 1) {
    throw "核心服务、Web BFF 和 React 前端不能使用相同端口。"
}

$workspaceItem = Get-Item -LiteralPath $WorkspacePath -ErrorAction Stop
if (-not $workspaceItem.PSIsContainer) {
    throw "工作区路径必须是目录：$WorkspacePath"
}
$workspaceFullPath = $workspaceItem.FullName

if (Test-Path -LiteralPath $manifestPath) {
    $previous = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $liveIds = @(
        $previous.core.pid,
        $previous.web.pid,
        $previous.frontend.pid
    ) | Where-Object {
        $null -ne (Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue)
    }
    if ($liveIds.Count -gt 0) {
        throw "开发服务已在运行（PID: $($liveIds -join ', ')）。请先执行 .\stop-multi-agent-dev.ps1。"
    }
    Remove-Item -LiteralPath $manifestPath -Force
}

Assert-PortCanBind -Address $ListenHost -Port $CorePort -ServiceName "核心服务"
Assert-PortCanBind -Address $ListenHost -Port $WebPort -ServiceName "Web BFF"
Assert-PortCanBind -Address $ListenHost -Port $FrontendPort -ServiceName "React 前端"
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

$stateDatabase = Join-Path $runtimeDir "state.sqlite3"
$requiredStateSchemaVersion = 5
if (Test-Path -LiteralPath $stateDatabase -PathType Leaf) {
    $detectedStateSchemaVersion = & $python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); row=c.execute('SELECT version FROM schema_metadata WHERE id=1').fetchone(); print(row[0] if row else 'missing'); c.close()" $stateDatabase 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "状态数据库结构无法识别：$stateDatabase。请先备份并移走该文件；启动脚本不会自动迁移或删除数据。"
    }
    if ([string]$detectedStateSchemaVersion -ne [string]$requiredStateSchemaVersion) {
        throw "状态数据库 schema v$detectedStateSchemaVersion 与核心要求的 v$requiredStateSchemaVersion 不兼容：$stateDatabase。请先备份并移走该文件；启动脚本不会自动迁移或删除数据。"
    }
}

$sessionStamp = Get-Date -Format "yyyyMMdd-HHmmssfff"
$coreOutLog = Join-Path $runtimeDir "core-$sessionStamp.stdout.log"
$coreErrorLog = Join-Path $runtimeDir "core-$sessionStamp.stderr.log"
$webOutLog = Join-Path $runtimeDir "web-$sessionStamp.stdout.log"
$webErrorLog = Join-Path $runtimeDir "web-$sessionStamp.stderr.log"
$frontendOutLog = Join-Path $runtimeDir "frontend-$sessionStamp.stdout.log"
$frontendErrorLog = Join-Path $runtimeDir "frontend-$sessionStamp.stderr.log"

if (-not $CodexBin) {
    $codexCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    }
    if ($null -ne $codexCommand) {
        $CodexBin = $codexCommand.Source
    }
}
if (-not $CodexHome) {
    if ($env:CODEX_HOME) {
        $CodexHome = $env:CODEX_HOME
    }
    else {
        $CodexHome = Join-Path $HOME ".codex"
    }
}

$workspaceMap = @{}
$workspaceMap[$WorkspaceId] = $workspaceFullPath
$env:MULTI_AGENT_WORKSPACES = ($workspaceMap | ConvertTo-Json -Compress)
$env:MULTI_AGENT_STATE_DB = $stateDatabase
$env:MULTI_AGENT_CORE_URL = "http://${ListenHost}:$CorePort"
$env:VITE_BFF_URL = "http://${ListenHost}:$WebPort"
$env:PYTHONUNBUFFERED = "1"
if ($CodexBin) {
    $env:MULTI_AGENT_CODEX_BIN = $CodexBin
}
if ($CodexHome) {
    $env:MULTI_AGENT_CODEX_HOME = $CodexHome
}

$coreArguments = @(
    "-m", "uvicorn",
    "multi_agent.main:app",
    "--app-dir", $coreAppDir,
    "--host", $ListenHost,
    "--port", "$CorePort",
    "--reload",
    "--reload-dir", $coreReloadDir,
    "--reload-include", "*.py",
    "--log-level", "info"
)
$webArguments = @(
    "-m", "uvicorn",
    "multi_agent_web.main:app",
    "--app-dir", $webAppDir,
    "--host", $ListenHost,
    "--port", "$WebPort",
    "--reload",
    "--reload-dir", $webReloadDir,
    "--reload-include", "*.py",
    "--reload-include", "*.html",
    "--reload-include", "*.css",
    "--reload-include", "*.js",
    "--log-level", "info"
)
$frontendArguments = @(
    "run", "dev", "--",
    "--host", $ListenHost,
    "--port", "$FrontendPort",
    "--strictPort"
)

$coreProcess = $null
$webProcess = $null
$frontendProcess = $null
$manifestWritten = $false

try {
    Write-Host "启动 Multi-Agent 核心服务……" -ForegroundColor Cyan
    $coreProcess = Start-Process `
        -FilePath $python `
        -ArgumentList $coreArguments `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $coreOutLog `
        -RedirectStandardError $coreErrorLog `
        -PassThru

    Write-Host "启动独立 Web BFF……" -ForegroundColor Cyan
    $webProcess = Start-Process `
        -FilePath $python `
        -ArgumentList $webArguments `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $webOutLog `
        -RedirectStandardError $webErrorLog `
        -PassThru

    Write-Host "启动 React 前端（Vite HMR）……" -ForegroundColor Cyan
    $frontendProcess = Start-Process `
        -FilePath $npmCommand.Source `
        -ArgumentList $frontendArguments `
        -WorkingDirectory $frontendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $frontendOutLog `
        -RedirectStandardError $frontendErrorLog `
        -PassThru

    $coreIdentity = Get-ProcessIdentity -Process $coreProcess
    $webIdentity = Get-ProcessIdentity -Process $webProcess
    $frontendIdentity = Get-ProcessIdentity -Process $frontendProcess
    $manifest = [ordered]@{
        schema_version = 3
        mode = if ($Detached) { "detached" } else { "supervised" }
        started_at = [DateTimeOffset]::Now.ToString("o")
        root = $root
        core = [ordered]@{
            pid = $coreIdentity.pid
            process_name = $coreIdentity.process_name
            started_at_utc = $coreIdentity.started_at_utc
            executable = $coreIdentity.executable
            app = "multi_agent.main:app"
            host = $ListenHost
            port = $CorePort
            stdout = $coreOutLog
            stderr = $coreErrorLog
        }
        web = [ordered]@{
            pid = $webIdentity.pid
            process_name = $webIdentity.process_name
            started_at_utc = $webIdentity.started_at_utc
            executable = $webIdentity.executable
            app = "multi_agent_web.main:app"
            host = $ListenHost
            port = $WebPort
            stdout = $webOutLog
            stderr = $webErrorLog
        }
        frontend = [ordered]@{
            pid = $frontendIdentity.pid
            process_name = $frontendIdentity.process_name
            started_at_utc = $frontendIdentity.started_at_utc
            executable = $frontendIdentity.executable
            app = "vite"
            host = $ListenHost
            port = $FrontendPort
            stdout = $frontendOutLog
            stderr = $frontendErrorLog
        }
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $manifestWritten = $true

    Wait-ServiceHealth `
        -ServiceName "核心服务" `
        -HealthUrl "http://${ListenHost}:$CorePort/health" `
        -Process $coreProcess `
        -ErrorLog $coreErrorLog `
        -TimeoutSeconds $StartupTimeoutSeconds
    Wait-ServiceHealth `
        -ServiceName "Web BFF" `
        -HealthUrl "http://${ListenHost}:$WebPort/health" `
        -Process $webProcess `
        -ErrorLog $webErrorLog `
        -TimeoutSeconds $StartupTimeoutSeconds
    Wait-ServiceHealth `
        -ServiceName "React 前端" `
        -HealthUrl "http://${ListenHost}:$FrontendPort/health" `
        -Process $frontendProcess `
        -ErrorLog $frontendErrorLog `
        -TimeoutSeconds $StartupTimeoutSeconds
}
catch {
    if ($manifestWritten) {
        try {
            & $stopScript -Quiet
        }
        catch {
            Write-Warning "自动清理失败，请执行 .\stop-multi-agent-dev.ps1。"
        }
    }
    else {
        Stop-NewProcessTree -Process $frontendProcess
        Stop-NewProcessTree -Process $webProcess
        Stop-NewProcessTree -Process $coreProcess
    }
    throw
}

Write-Host ""
Write-Host "Multi-Agent 开发环境已就绪。" -ForegroundColor Green
Write-Host "  核心服务: http://${ListenHost}:$CorePort  (PID $($coreProcess.Id))"
Write-Host "  Web BFF:  http://${ListenHost}:$WebPort  (PID $($webProcess.Id))"
Write-Host "  前端页面: http://${ListenHost}:$FrontendPort  (PID $($frontendProcess.Id))"
Write-Host "  工作区:   $WorkspaceId -> $workspaceFullPath"
Write-Host "  运行清单: $manifestPath"
Write-Host "  日志目录: $runtimeDir"

if ($Detached) {
    Write-Host ""
    Write-Host "服务已在后台运行。停止时执行：.\stop-multi-agent-dev.ps1" -ForegroundColor Yellow
    return
}

Write-Host ""
Write-Host "监督模式正在运行；Python 服务启用 reload，React 前端启用 Vite HMR。" -ForegroundColor Yellow
Write-Host "按 Ctrl+C 可停止三个服务，也可以在另一终端执行 .\stop-multi-agent-dev.ps1。"

$nextHeartbeat = [DateTime]::UtcNow.AddSeconds($HeartbeatSeconds)
try {
    while ($true) {
        $coreProcess.Refresh()
        $webProcess.Refresh()
        $frontendProcess.Refresh()

        if ($coreProcess.HasExited -or $webProcess.HasExited -or $frontendProcess.HasExited) {
            if (-not (Test-Path -LiteralPath $manifestPath)) {
                Write-Host "服务已由停止脚本结束。" -ForegroundColor Yellow
                break
            }
            if ($coreProcess.HasExited) {
                Show-RecentLog -ServiceName "核心服务" -ErrorLog $coreErrorLog
                throw "核心服务意外退出，退出码：$($coreProcess.ExitCode)"
            }
            if ($webProcess.HasExited) {
                Show-RecentLog -ServiceName "Web BFF" -ErrorLog $webErrorLog
                throw "Web BFF 意外退出，退出码：$($webProcess.ExitCode)"
            }
            Show-RecentLog -ServiceName "React 前端" -ErrorLog $frontendErrorLog
            throw "React 前端意外退出，退出码：$($frontendProcess.ExitCode)"
        }

        if ([DateTime]::UtcNow -ge $nextHeartbeat) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 核心、Web BFF 与 React 前端运行正常。"
            $nextHeartbeat = [DateTime]::UtcNow.AddSeconds($HeartbeatSeconds)
        }
        Start-Sleep -Milliseconds 500
    }
}
finally {
    if (Test-Path -LiteralPath $manifestPath) {
        Write-Host ""
        Write-Host "正在停止 Multi-Agent 开发服务……" -ForegroundColor Cyan
        & $stopScript -Quiet
    }
}
