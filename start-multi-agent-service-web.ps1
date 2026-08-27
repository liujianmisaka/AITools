[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ManagementPort = 8014,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5174,
    [ValidateRange(1, 65535)]
    [int]$ControlPlanePort = 8016,
    [ValidateRange(1, 65535)]
    [int]$MainWebPort = 5173,
    [ValidateRange(1, 65535)]
    [int]$CoordinatorPort = 8020,
    [string]$ConfigurationPath,
    [switch]$SkipReadyCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendRoot = Join-Path $root "multi-agent-v3"
$serviceWebRoot = Join-Path $root "multi-agent-service-web"
$managementSource = Join-Path $serviceWebRoot "backend"
$runtimeRoot = Join-Path $root ".tmp\multi-agent-service-web-runtime"
$manifestPath = Join-Path $runtimeRoot "services.json"

function Get-ProcessStartUnixMilliseconds([System.Diagnostics.Process]$Process) {
    return ([DateTimeOffset]$Process.StartTime).ToUnixTimeMilliseconds()
}

function Stop-ProcessTree([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $taskkillOptions = @{
        FilePath = Join-Path $env:SystemRoot "System32\taskkill.exe"
        ArgumentList = @('/PID', "$ProcessId", '/T', '/F')
        WindowStyle = 'Hidden'
        Wait = $true
        PassThru = $true
    }
    $taskkill = Start-Process @taskkillOptions
    if ($taskkill.ExitCode -ne 0 -or (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Stop-RecordedProcess([object]$Entry) {
    $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $differenceMilliseconds = [Math]::Abs(
        (Get-ProcessStartUnixMilliseconds -Process $process) -
        [long]$Entry.startTimeUnixMilliseconds
    )
    if ($differenceMilliseconds -gt 2000) {
        throw "Recorded PID $($Entry.pid) now belongs to a different process."
    }
    Stop-ProcessTree -ProcessId ([int]$Entry.pid)
    if (Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue) {
        throw "Recorded process $($Entry.pid) could not be stopped."
    }
}

function Assert-PortFree([int]$Port, [string]$Role) {
    $pattern = "\s+\S+:$Port\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$"
    $listeners = @(netstat -ano -p tcp | Select-String -Pattern $pattern)
    if ($listeners.Count -gt 0) {
        $listenerPid = $listeners[0].Matches[0].Groups['pid'].Value
        throw "$Role port $Port is already in use by PID $listenerPid. Stop the existing AITools service first."
    }
}

function Assert-StartedProcess(
    [System.Diagnostics.Process]$Process,
    [string]$Role,
    [string]$ErrorLog
) {
    Start-Sleep -Milliseconds 300
    if (-not $Process.HasExited) {
        return
    }
    $detail = Get-Content -LiteralPath $ErrorLog -Tail 30 -ErrorAction SilentlyContinue
    throw "$Role exited during startup. Details: $($detail -join [Environment]::NewLine)"
}

function Wait-Ready([string]$Url, [string]$Role, [string]$ErrorLog) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        try {
            $response = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $deadline)
    $detail = Get-Content -LiteralPath $ErrorLog -Tail 30 -ErrorAction SilentlyContinue
    throw "$Role did not become ready. Details: $($detail -join [Environment]::NewLine)"
}

$selectedPorts = @(
    $ManagementPort,
    $FrontendPort,
    $ControlPlanePort,
    $MainWebPort,
    $CoordinatorPort
)
if (($selectedPorts | Sort-Object -Unique).Count -ne $selectedPorts.Count) {
    throw "Management, service web, Control Plane, Coordinator, and main web ports must be distinct."
}

New-Item -ItemType Directory -Force $runtimeRoot | Out-Null
if (Test-Path -LiteralPath $manifestPath) {
    $existing = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($existing.managementUrl) {
        try {
            Invoke-RestMethod -Method Post "$($existing.managementUrl)/groups/all/stop" -TimeoutSec 20 | Out-Null
        } catch { }
    }
    foreach ($entry in @($existing.services | Sort-Object role -Descending)) {
        Stop-RecordedProcess -Entry $entry
    }
    Remove-Item -LiteralPath $manifestPath -Force
}

Assert-PortFree -Port $ManagementPort -Role "Management API"
Assert-PortFree -Port $FrontendPort -Role "Service Web"
Assert-PortFree -Port $ControlPlanePort -Role "Control Plane"
Assert-PortFree -Port $MainWebPort -Role "Main Web"
Assert-PortFree -Port $CoordinatorPort -Role "Coordinator"

$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "The V3 Python environment is missing. Run uv sync --all-packages in multi-agent-v3 first."
}
$node = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $serviceWebRoot "node_modules\vite\bin\vite.js"
if (-not (Test-Path -LiteralPath $viteEntry)) {
    throw "Service Web dependencies are missing. Run npm ci in multi-agent-service-web first."
}

$managementUrl = "http://127.0.0.1:$ManagementPort"
$backendLog = Join-Path $runtimeRoot "management.out.log"
$backendErrorLog = Join-Path $runtimeRoot "management.err.log"
$frontendLog = Join-Path $runtimeRoot "frontend.out.log"
$frontendErrorLog = Join-Path $runtimeRoot "frontend.err.log"
$backend = $null
$frontend = $null
$previousPythonPath = $env:PYTHONPATH
$previousPythonUtf8 = $env:PYTHONUTF8
$previousProxyTarget = $env:VITE_API_PROXY_TARGET

try {
    $env:PYTHONPATH = $managementSource
    $env:PYTHONUTF8 = "1"
    $backendArguments = @(
        "-m", "aitools_service_manager",
        "--root", $root,
        "--host", "127.0.0.1",
        "--port", $ManagementPort,
        "--service-web-port", $FrontendPort,
        "--control-plane-port", $ControlPlanePort,
        "--main-web-port", $MainWebPort,
        "--coordinator-port", $CoordinatorPort
    )
    if ($ConfigurationPath) {
        $backendArguments += @("--configuration-path", $ConfigurationPath)
    }
    $backendOptions = @{
        FilePath = $python
        WorkingDirectory = $serviceWebRoot
        ArgumentList = $backendArguments
        RedirectStandardOutput = $backendLog
        RedirectStandardError = $backendErrorLog
        WindowStyle = 'Hidden'
        PassThru = $true
    }
    $backend = Start-Process @backendOptions
    Assert-StartedProcess -Process $backend -Role "Management API" -ErrorLog $backendErrorLog
    if (-not $SkipReadyCheck) {
        Wait-Ready -Url "$managementUrl/ready" -Role "Management API" -ErrorLog $backendErrorLog
    }

    $env:VITE_API_PROXY_TARGET = $managementUrl
    $frontendOptions = @{
        FilePath = $node
        WorkingDirectory = $serviceWebRoot
        ArgumentList = @($viteEntry, '--host', '127.0.0.1', '--port', $FrontendPort, '--strictPort')
        RedirectStandardOutput = $frontendLog
        RedirectStandardError = $frontendErrorLog
        WindowStyle = 'Hidden'
        PassThru = $true
    }
    $frontend = Start-Process @frontendOptions
    Assert-StartedProcess -Process $frontend -Role "Service Web" -ErrorLog $frontendErrorLog
    if (-not $SkipReadyCheck) {
        Wait-Ready -Url "http://127.0.0.1:$FrontendPort/" -Role "Service Web" -ErrorLog $frontendErrorLog
    }

    [ordered]@{
        version = 3
        managementUrl = $managementUrl
        serviceWebUrl = "http://127.0.0.1:$FrontendPort"
        services = @(
            [ordered]@{
                role = "management-api"
                pid = $backend.Id
                startTimeUnixMilliseconds = Get-ProcessStartUnixMilliseconds -Process $backend
                port = $ManagementPort
            },
            [ordered]@{
                role = "service-web"
                pid = $frontend.Id
                startTimeUnixMilliseconds = Get-ProcessStartUnixMilliseconds -Process $frontend
                port = $FrontendPort
            }
        )
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
} catch {
    Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    if ($frontend) {
        Stop-ProcessTree -ProcessId $frontend.Id
    }
    if ($backend) {
        Stop-ProcessTree -ProcessId $backend.Id
    }
    throw
} finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($null -eq $previousPythonUtf8) {
        Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONUTF8 = $previousPythonUtf8
    }
    if ($null -eq $previousProxyTarget) {
        Remove-Item Env:VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue
    } else {
        $env:VITE_API_PROXY_TARGET = $previousProxyTarget
    }
}

Write-Host "AITools Manager: $managementUrl"
Write-Host "Service Web:     http://127.0.0.1:$FrontendPort"
Write-Host "Managed targets: Control Plane $ControlPlanePort, Coordinator $CoordinatorPort, Main Web $MainWebPort"
Write-Host "Runtime config:  Configure and save it in Service Web before starting core services"
Write-Host "Logs:            $runtimeRoot"
