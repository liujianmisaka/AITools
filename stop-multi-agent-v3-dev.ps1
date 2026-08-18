[CmdletBinding()]
param(
    [int]$BackendPort = 8016,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "SilentlyContinue"

foreach ($port in @($BackendPort, $FrontendPort)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        Stop-Process -Id $listener.OwningProcess -Force
    }
}

Write-Host "Stopped V3 services on ports $BackendPort and $FrontendPort."
