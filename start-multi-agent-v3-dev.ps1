[CmdletBinding()]
param(
    [int]$BackendPort = 8016,
    [int]$FrontendPort = 5173,
    [ValidateSet("fake", "codex")]
    [string]$Profile = "fake",
    [string]$CodexHome,
    [string[]]$WorkspaceRoot,
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $Root "multi-agent-v3"
$FrontendRoot = Join-Path $Root "multi-agent-web-v3"
$RuntimeRoot = Join-Path $Root ".tmp\multi-agent-v3-runtime"
$env:UV_CACHE_DIR = Join-Path $BackendRoot ".tmp-uv-cache"
New-Item -ItemType Directory -Force $RuntimeRoot | Out-Null

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

function Get-ProcessStartTimeUtc([System.Diagnostics.Process]$Process) {
    return $Process.StartTime.ToUniversalTime().ToString('O')
}

function Assert-StartedProcess(
    [System.Diagnostics.Process]$Process,
    [string]$Role,
    [string]$ErrorLog
) {
    Start-Sleep -Milliseconds 250
    if (-not $Process.HasExited) {
        return
    }
    $detail = Get-Content -LiteralPath $ErrorLog -Tail 30 -ErrorAction SilentlyContinue
    throw "$Role exited during startup.`n$($detail -join [Environment]::NewLine)"
}

$manifestPath = Join-Path $RuntimeRoot "services.json"
$previousProxyTarget = $env:VITE_API_PROXY_TARGET
$backend = $null
$frontend = $null

function Assert-PortFree([int]$Port) {
    $listeners = @(netstat -ano -p tcp | Select-String -Pattern ("\s+\S+:$Port\s+\S+\s+LISTENING\s+(?<pid>\d+)\s*$"))
    if ($listeners.Count -gt 0) {
        $listenerPid = $listeners[0].Matches[0].Groups['pid'].Value
        throw "Port $Port is already in use by PID $listenerPid."
    }
}

if (Test-Path -LiteralPath $manifestPath) {
    try {
        $existing = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        foreach ($entry in @($existing.services)) {
            Stop-ProcessTree -ProcessId ([int]$entry.pid)
        }
    } catch {
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    }
}
Assert-PortFree $BackendPort
Assert-PortFree $FrontendPort

if ($Profile -eq "codex") {
    if (-not $CodexHome) { throw "-CodexHome is required when -Profile codex is selected." }
    if (-not $WorkspaceRoot -or $WorkspaceRoot.Count -eq 0) {
        throw "At least one -WorkspaceRoot is required when -Profile codex is selected."
    }
}

$backendLog = Join-Path $RuntimeRoot "backend.out.log"
$backendErrorLog = Join-Path $RuntimeRoot "backend.err.log"
$frontendLog = Join-Path $RuntimeRoot "frontend.out.log"
$frontendErrorLog = Join-Path $RuntimeRoot "frontend.err.log"
try {
    $env:VITE_API_PROXY_TARGET = "http://127.0.0.1:$BackendPort"
    if (-not (Test-Path -LiteralPath (Join-Path $BackendRoot '.venv\Scripts\python.exe'))) {
        throw "The V3 Python environment is missing. Run 'uv sync --all-packages' in multi-agent-v3 first."
    }
    $backendPython = Join-Path $BackendRoot '.venv\Scripts\python.exe'
    if ($Profile -eq "fake") {
        $backendArguments = @(
            "examples/control_plane_fake.py", "--host", "127.0.0.1", "--port", $BackendPort
        )
    } else {
        $backendArguments = @(
            "examples/control_plane_codex.py",
            "--host", "127.0.0.1", "--port", $BackendPort,
            "--codex-home", $CodexHome
        )
        foreach ($root in $WorkspaceRoot) {
            $backendArguments += @("--workspace-root", $root)
        }
    }
    $backend = Start-Process -FilePath $backendPython -WorkingDirectory $BackendRoot -ArgumentList $backendArguments -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog -WindowStyle Hidden -PassThru
    Assert-StartedProcess -Process $backend -Role 'Backend' -ErrorLog $backendErrorLog
    $nodePath = (Get-Command node.exe -ErrorAction Stop).Source
    $viteEntry = Join-Path $FrontendRoot 'node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $viteEntry)) {
        throw "Vite is not installed. Run 'npm ci' in multi-agent-web-v3 first."
    }
    $frontend = Start-Process -FilePath $nodePath -WorkingDirectory $FrontendRoot -ArgumentList @(
        $viteEntry, "--host", "127.0.0.1", "--port", $FrontendPort
    ) -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog -WindowStyle Hidden -PassThru
    Assert-StartedProcess -Process $frontend -Role 'Frontend' -ErrorLog $frontendErrorLog
} catch {
    if ($frontend) { Stop-ProcessTree -ProcessId $frontend.Id }
    if ($backend) { Stop-ProcessTree -ProcessId $backend.Id }
    throw
} finally {
    if ($null -eq $previousProxyTarget) {
        Remove-Item Env:VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue
    } else {
        $env:VITE_API_PROXY_TARGET = $previousProxyTarget
    }
}

@{
    version = 1
    services = @(
        [ordered]@{
            role = "backend"
            pid = $backend.Id
            startTimeUtc = Get-ProcessStartTimeUtc $backend
            port = $BackendPort
        },
        [ordered]@{
            role = "frontend"
            pid = $frontend.Id
            startTimeUtc = Get-ProcessStartTimeUtc $frontend
            port = $FrontendPort
        }
    )
    profile = $Profile
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (-not $NoWait) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        try {
            $ready = Invoke-RestMethod "http://127.0.0.1:$BackendPort/ready" -TimeoutSec 2
            if ($ready.status -eq "ready") { break }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if (-not $ready) {
        Stop-ProcessTree -ProcessId $frontend.Id
        Stop-ProcessTree -ProcessId $backend.Id
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
        throw "Control Plane did not become ready. See $backendLog"
    }
}

Write-Host "Control Plane: http://127.0.0.1:$BackendPort"
Write-Host "Web UI:        http://127.0.0.1:$FrontendPort"
Write-Host "Logs:          $RuntimeRoot"
