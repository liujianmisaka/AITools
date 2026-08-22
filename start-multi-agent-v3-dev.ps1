[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8016,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 5173,
    [ValidateRange(1, 65535)]
    [int]$ManagementPort = 8014,
    [ValidateRange(1, 65535)]
    [int]$ServiceWebPort = 5174,
    [ValidateSet("fake", "codex")]
    [string]$Profile = "fake",
    [string]$CodexHome,
    [string[]]$WorkspaceRoot,
    [string[]]$WorkspaceId,
    [string]$StatePath,
    [string]$ProviderId = "codex",
    [switch]$NetworkDenyEnforced
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$managerScript = Join-Path $root "start-multi-agent-service-web.ps1"
$parameters = @{
    ManagementPort = $ManagementPort
    FrontendPort = $ServiceWebPort
    ControlPlanePort = $BackendPort
    MainWebPort = $FrontendPort
    Profile = $Profile
    ProviderId = $ProviderId
}
if ($CodexHome) {
    $parameters.CodexHome = $CodexHome
}
if ($WorkspaceRoot) {
    $parameters.WorkspaceRoot = $WorkspaceRoot
}
if ($WorkspaceId) {
    $parameters.WorkspaceId = $WorkspaceId
}
if ($StatePath) {
    $parameters.StatePath = $StatePath
}
if ($NetworkDenyEnforced) {
    $parameters.NetworkDenyEnforced = $true
}

& $managerScript @parameters
$managementUrl = "http://127.0.0.1:$ManagementPort"
$result = Invoke-RestMethod -Method Post "$managementUrl/groups/core/start" -TimeoutSec 60
$controlPlane = $result.services | Where-Object service_id -eq "control-plane"
$mainWeb = $result.services | Where-Object service_id -eq "web-v3"

Write-Host "Control Plane: $($controlPlane.endpoint) [$($controlPlane.status)]"
Write-Host "Main Web:      $($mainWeb.endpoint) [$($mainWeb.status)]"
Write-Host "Service Web:   http://127.0.0.1:$ServiceWebPort"
