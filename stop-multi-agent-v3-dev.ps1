[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ManagementPort = 8014,
    [ValidateRange(1, 65535)]
    [int]$ServiceWebPort = 5174
)

$ErrorActionPreference = "Stop"
$managementUrl = "http://127.0.0.1:$ManagementPort"
try {
    $result = Invoke-RestMethod -Method Post "$managementUrl/groups/core/stop" -TimeoutSec 30
} catch {
    throw "Failed to stop the V3 core through ${managementUrl}: $($_.Exception.Message)"
}

$controlPlane = $result.services | Where-Object service_id -eq "control-plane"
$codexAppServer = $result.services | Where-Object service_id -eq "codex-app-server"
$coordinator = $result.services | Where-Object service_id -eq "multi-agent-coordinator"
$mainWeb = $result.services | Where-Object service_id -eq "web-v3"
Write-Host "Control Plane: $($controlPlane.status)"
Write-Host "Codex App Server: $($codexAppServer.status)"
Write-Host "Coordinator:   $($coordinator.status)"
Write-Host "Main Web:      $($mainWeb.status)"
Write-Host "AITools Service Web remains available at http://127.0.0.1:$ServiceWebPort"
