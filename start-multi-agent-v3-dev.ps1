[CmdletBinding()]
param(
    [int]$BackendPort = 8016,
    [int]$FrontendPort = 5173,
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $Root "multi-agent-v3"
$FrontendRoot = Join-Path $Root "multi-agent-web-v3"
$RuntimeRoot = Join-Path $Root ".tmp\multi-agent-v3-runtime"
$env:UV_CACHE_DIR = Join-Path $BackendRoot ".tmp-uv-cache"
New-Item -ItemType Directory -Force $RuntimeRoot | Out-Null

function Assert-PortFree([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port is already in use by PID $($listener[0].OwningProcess)." }
}

Assert-PortFree $BackendPort
Assert-PortFree $FrontendPort

$backendLog = Join-Path $RuntimeRoot "backend.out.log"
$backendErrorLog = Join-Path $RuntimeRoot "backend.err.log"
$frontendLog = Join-Path $RuntimeRoot "frontend.out.log"
$frontendErrorLog = Join-Path $RuntimeRoot "frontend.err.log"
try {
    $backend = Start-Process -FilePath "uv" -WorkingDirectory $BackendRoot -ArgumentList @(
        "run", "python", "examples/control_plane_fake.py", "--host", "127.0.0.1", "--port", $BackendPort
    ) -RedirectStandardOutput $backendLog -RedirectStandardError $backendErrorLog -WindowStyle Hidden -PassThru
    $npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
    $frontend = Start-Process -FilePath $npmPath -WorkingDirectory $FrontendRoot -ArgumentList @(
        "run", "dev", "--", "--port", $FrontendPort
    ) -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErrorLog -WindowStyle Hidden -PassThru
} catch {
    if ($backend) { Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue }
    throw
}

@{
    BackendPid = $backend.Id
    FrontendPid = $frontend.Id
    BackendPort = $BackendPort
    FrontendPort = $FrontendPort
} | ConvertTo-Json | Set-Content (Join-Path $RuntimeRoot "services.json") -Encoding UTF8

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
        throw "Control Plane did not become ready. See $backendLog"
    }
}

Write-Host "Control Plane: http://127.0.0.1:$BackendPort"
Write-Host "Web UI:        http://127.0.0.1:$FrontendPort"
Write-Host "Logs:          $RuntimeRoot"
