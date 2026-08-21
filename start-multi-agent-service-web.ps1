[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5174,
    [string]$ControlPlaneUrl = "http://127.0.0.1:8016",
    [switch]$SkipReadyCheck
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$frontendRoot = Join-Path $root "multi-agent-service-web"
$runtimeRoot = Join-Path $root ".tmp\multi-agent-service-web-runtime"
$manifestPath = Join-Path $runtimeRoot "service.json"

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
    $actualStart = [DateTimeOffset]$process.StartTime
    $differenceMilliseconds = [Math]::Abs(
        $actualStart.ToUnixTimeMilliseconds() - [long]$Entry.startTimeUnixMilliseconds
    )
    if ($differenceMilliseconds -gt 2000) {
        throw "Recorded PID now belongs to a different process; it was not stopped."
    }
    Stop-ProcessTree -ProcessId ([int]$Entry.pid)
    if (Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue) {
        throw "The recorded Service Web process could not be stopped."
    }
}

function Assert-PortFree([int]$Port) {
    $pattern = "\s+\S+:$Port\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$"
    $listeners = @(netstat -ano -p tcp | Select-String -Pattern $pattern)
    if ($listeners.Count -gt 0) {
        $listenerPid = $listeners[0].Matches[0].Groups['pid'].Value
        throw "Port $Port is already in use by PID $listenerPid."
    }
}

$controlPlaneUri = [Uri]$ControlPlaneUrl
if (-not $controlPlaneUri.IsAbsoluteUri -or $controlPlaneUri.Scheme -notin @('http', 'https')) {
    throw "-ControlPlaneUrl must be an absolute HTTP or HTTPS URL."
}
$proxyTarget = $ControlPlaneUrl.TrimEnd('/')

New-Item -ItemType Directory -Force $runtimeRoot | Out-Null
if (Test-Path -LiteralPath $manifestPath) {
    $existing = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Stop-RecordedProcess -Entry $existing
    Remove-Item -LiteralPath $manifestPath -Force
}
Assert-PortFree -Port $FrontendPort

$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
$viteEntry = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
if (-not (Test-Path -LiteralPath $viteEntry)) {
    throw "Frontend dependencies are missing. Run npm install in multi-agent-service-web first."
}

$frontendLog = Join-Path $runtimeRoot "frontend.out.log"
$frontendErrorLog = Join-Path $runtimeRoot "frontend.err.log"
$previousProxyTarget = $env:VITE_API_PROXY_TARGET
$frontend = $null
try {
    $env:VITE_API_PROXY_TARGET = $proxyTarget
    $frontendOptions = @{
        FilePath = $nodePath
        WorkingDirectory = $frontendRoot
        ArgumentList = @($viteEntry, '--host', '127.0.0.1', '--port', $FrontendPort, '--strictPort')
        RedirectStandardOutput = $frontendLog
        RedirectStandardError = $frontendErrorLog
        WindowStyle = 'Hidden'
        PassThru = $true
    }
    $frontend = Start-Process @frontendOptions
    Start-Sleep -Milliseconds 300
    if ($frontend.HasExited) {
        $detail = Get-Content -LiteralPath $frontendErrorLog -Tail 30 -ErrorAction SilentlyContinue
        throw "Service Web exited during startup. Details: $($detail -join [Environment]::NewLine)"
    }
} catch {
    if ($frontend) {
        Stop-ProcessTree -ProcessId $frontend.Id
    }
    throw
} finally {
    if ($null -eq $previousProxyTarget) {
        Remove-Item Env:VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue
    } else {
        $env:VITE_API_PROXY_TARGET = $previousProxyTarget
    }
}

[ordered]@{
    version = 1
    pid = $frontend.Id
    startTimeUnixMilliseconds = ([DateTimeOffset]$frontend.StartTime).ToUnixTimeMilliseconds()
    port = $FrontendPort
    controlPlaneUrl = $proxyTarget
} | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (-not $SkipReadyCheck) {
    $ready = $false
    $deadline = (Get-Date).AddSeconds(20)
    do {
        try {
            $response = Invoke-WebRequest "http://127.0.0.1:$FrontendPort/" -UseBasicParsing -TimeoutSec 2
            $ready = $response.StatusCode -eq 200
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } while (-not $ready -and (Get-Date) -lt $deadline)
    if (-not $ready) {
        Stop-ProcessTree -ProcessId $frontend.Id
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
        throw "Service Web did not become ready. See $frontendErrorLog"
    }
}

Write-Host "Service Web:   http://127.0.0.1:$FrontendPort"
Write-Host "Control Plane: $proxyTarget"
Write-Host "Logs:          $runtimeRoot"
