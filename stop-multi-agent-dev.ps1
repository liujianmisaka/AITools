[CmdletBinding()]
param(
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Test-ProcessIdentity {
    param(
        [System.Diagnostics.Process]$Process,
        [object]$Entry,
        [string]$ServiceName
    )

    $Process.Refresh()
    if ($Process.ProcessName -ne [string]$Entry.process_name) {
        Write-Warning "$ServiceName 的 PID $($Process.Id) 已被其他进程占用，进程名不匹配。"
        return $false
    }

    $recordedStart = $Entry.started_at_utc
    $expectedStart = if ($recordedStart -is [DateTime]) {
        $recordedStart.ToUniversalTime()
    }
    else {
        [DateTimeOffset]::Parse(
            [string]$recordedStart,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).UtcDateTime
    }
    $actualStart = $Process.StartTime.ToUniversalTime()
    if ([Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -gt 2) {
        Write-Warning "$ServiceName 的 PID $($Process.Id) 已被复用，启动时间不匹配。"
        return $false
    }

    $expectedExecutable = [string]$Entry.executable
    if ($expectedExecutable) {
        $actualExecutable = ""
        try {
            $actualExecutable = $Process.Path
        }
        catch {
            Write-Warning "无法读取 $ServiceName 的可执行文件路径，未执行停止操作。"
            return $false
        }

        if (
            -not [string]::Equals(
                $actualExecutable,
                $expectedExecutable,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Write-Warning "$ServiceName 的 PID $($Process.Id) 已被其他进程占用，可执行文件不匹配。"
            return $false
        }
    }

    return $true
}

function Stop-TrackedService {
    param(
        [string]$ServiceName,
        [object]$Entry,
        [switch]$SuppressOutput
    )

    $rootProcessId = [int]$Entry.pid
    $process = Get-Process -Id $rootProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        if (-not $SuppressOutput) {
            Write-Host "$ServiceName 已停止（记录的 PID $rootProcessId 不存在）。"
        }
        return $true
    }

    if (-not (Test-ProcessIdentity -Process $process -Entry $Entry -ServiceName $ServiceName)) {
        return $false
    }

    $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
    $taskkillOutput = & $taskkill /PID $rootProcessId /T /F 2>&1
    $taskkillExitCode = $LASTEXITCODE

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($null -eq (Get-Process -Id $rootProcessId -ErrorAction SilentlyContinue)) {
            if (-not $SuppressOutput) {
                Write-Host "$ServiceName 已停止（根 PID: $rootProcessId）。"
            }
            return $true
        }
        Start-Sleep -Milliseconds 200
    }

    if ($taskkillExitCode -ne 0) {
        Write-Warning "$ServiceName 停止失败：$($taskkillOutput -join ' ')"
    }
    else {
        Write-Warning "$ServiceName 的根 PID $rootProcessId 在等待后仍然存在。"
    }
    return $false
}

function Test-PortAcceptingConnections {
    param(
        [string]$Address,
        [int]$Port
    )

    $targetAddress = if ($Address -in @("0.0.0.0", "::")) {
        "127.0.0.1"
    }
    else {
        $Address
    }

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $result = $client.BeginConnect($targetAddress, $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(350)) {
            return $false
        }
        $client.EndConnect($result)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$runtimeDir = Join-Path $root ".multi-agent-dev"
$manifestPath = Join-Path $runtimeDir "processes.json"

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    if (-not $Quiet) {
        Write-Host "没有找到运行清单，开发服务当前未由脚本跟踪：$manifestPath"
    }
    return
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ([int]$manifest.schema_version -ne 4) {
    throw "不支持的运行清单版本。请先人工核对进程，不会自动删除清单。"
}

$frontendStopped = Stop-TrackedService `
    -ServiceName "React 前端" `
    -Entry $manifest.frontend `
    -SuppressOutput:$Quiet
$webStopped = Stop-TrackedService `
    -ServiceName "Web BFF" `
    -Entry $manifest.web `
    -SuppressOutput:$Quiet
$coreStopped = Stop-TrackedService `
    -ServiceName "核心服务" `
    -Entry $manifest.core `
    -SuppressOutput:$Quiet

if (-not ($frontendStopped -and $webStopped -and $coreStopped)) {
    throw "至少一个服务未能安全停止，运行清单已保留：$manifestPath"
}

Remove-Item -LiteralPath $manifestPath -Force

if (
    Test-PortAcceptingConnections `
        -Address ([string]$manifest.frontend.host) `
        -Port ([int]$manifest.frontend.port)
) {
    Write-Warning "React 前端端口 $($manifest.frontend.port) 仍可连接，可能由未跟踪进程占用。"
}
if (
    Test-PortAcceptingConnections `
        -Address ([string]$manifest.web.host) `
        -Port ([int]$manifest.web.port)
) {
    Write-Warning "Web BFF 端口 $($manifest.web.port) 仍可连接，可能由未跟踪进程占用。"
}
if (
    Test-PortAcceptingConnections `
        -Address ([string]$manifest.core.host) `
        -Port ([int]$manifest.core.port)
) {
    Write-Warning "核心端口 $($manifest.core.port) 仍可连接，可能由未跟踪进程占用。"
}

if (-not $Quiet) {
    Write-Host "Multi-Agent 开发环境已停止。日志仍保留在：$runtimeDir" -ForegroundColor Green
}
