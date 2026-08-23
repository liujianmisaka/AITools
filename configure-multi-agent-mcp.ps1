[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Low")]
param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$Name = "multi_agent_v3",
    [ValidateNotNullOrEmpty()]
    [string]$ControlPlaneUrl = "http://127.0.0.1:8016",
    [ValidateSet("read_only", "workspace_write")]
    [string]$Sandbox = "workspace_write",
    [ValidateSet("allow", "deny")]
    [string]$NetworkPolicy = "deny",
    [ValidateRange(1, 3600)]
    [int]$RequestTimeoutSeconds = 30,
    [ValidateNotNullOrEmpty()]
    [string]$CodexCommand = "codex"
)

$ErrorActionPreference = "Stop"

function Invoke-CodexCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Executable,
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    $output = @(& $Executable @ArgumentList 2>&1)
    $exitCode = $LASTEXITCODE
    $outputText = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    if ($exitCode -ne 0) {
        throw "Codex command failed with exit code ${exitCode}: $outputText"
    }
    return $outputText
}

function Test-StringSequenceEqual {
    param(
        [AllowEmptyCollection()]
        [object[]]$Actual,
        [AllowEmptyCollection()]
        [string[]]$Expected
    )

    if ($Actual.Count -ne $Expected.Count) {
        return $false
    }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        if ([string]$Actual[$index] -cne $Expected[$index]) {
            return $false
        }
    }
    return $true
}

$parsedControlPlaneUrl = $null
$trimmedControlPlaneUrl = $ControlPlaneUrl.Trim()
$isAbsoluteUrl = [Uri]::TryCreate(
    $trimmedControlPlaneUrl,
    [UriKind]::Absolute,
    [ref]$parsedControlPlaneUrl
)
if (
    -not $isAbsoluteUrl -or
    $parsedControlPlaneUrl.Scheme -notin @("http", "https") -or
    -not $parsedControlPlaneUrl.Host -or
    $parsedControlPlaneUrl.UserInfo -or
    $parsedControlPlaneUrl.Query -or
    $parsedControlPlaneUrl.Fragment
) {
    throw "ControlPlaneUrl must be an absolute HTTP(S) base URL without credentials, query, or fragment."
}
$normalizedControlPlaneUrl = $trimmedControlPlaneUrl.TrimEnd("/")

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidate = Join-Path $root "multi-agent-v3\.venv\Scripts\python.exe"
$gatewaySourceCandidate = Join-Path $root "multi-agent-mcp\src"
$gatewayModule = Join-Path $gatewaySourceCandidate "misaka_mcp_gateway\__main__.py"
if (-not (Test-Path -LiteralPath $pythonCandidate -PathType Leaf)) {
    throw "The V3 Python environment is missing. Run 'uv sync --all-packages' in multi-agent-v3 first."
}
if (-not (Test-Path -LiteralPath $gatewayModule -PathType Leaf)) {
    throw "The Multi-Agent MCP gateway module is missing: $gatewayModule"
}

$pythonPath = (Resolve-Path -LiteralPath $pythonCandidate).Path
$gatewaySourcePath = (Resolve-Path -LiteralPath $gatewaySourceCandidate).Path
$codex = Get-Command -Name $CodexCommand -ErrorAction Stop | Select-Object -First 1
$codexExecutable = if ($codex.Source) { $codex.Source } else { $codex.Definition }

$gatewayArguments = @(
    "-m", "misaka_mcp_gateway",
    "--control-plane-url", $normalizedControlPlaneUrl,
    "--sandbox", $Sandbox,
    "--network-policy", $NetworkPolicy,
    "--timeout-seconds", [string]$RequestTimeoutSeconds
)
$expectedEnvironment = [ordered]@{
    PYTHONPATH = $gatewaySourcePath
    PYTHONUTF8 = "1"
}
$addArguments = @(
    "mcp", "add", $Name,
    "--env", "PYTHONPATH=$gatewaySourcePath",
    "--env", "PYTHONUTF8=1",
    "--", $pythonPath
) + $gatewayArguments

if (-not $PSCmdlet.ShouldProcess(
    "global Codex MCP server '$Name'",
    "register the Multi-Agent V3 STDIO gateway"
)) {
    return
}

$addOutput = Invoke-CodexCommand -Executable $codexExecutable -ArgumentList $addArguments
if ($addOutput) {
    Write-Host $addOutput
}

$getArguments = @("mcp", "get", $Name, "--json")
$configurationJson = Invoke-CodexCommand -Executable $codexExecutable -ArgumentList $getArguments
try {
    $configuration = $configurationJson | ConvertFrom-Json -ErrorAction Stop
} catch {
    throw "Codex returned invalid JSON while verifying MCP server '$Name': $configurationJson"
}

$transport = $configuration.transport
if (
    $configuration.name -ne $Name -or
    $configuration.enabled -ne $true -or
    $null -eq $transport -or
    $transport.type -ne "stdio" -or
    $transport.command -ne $pythonPath -or
    $null -eq $transport.env -or
    -not (Test-StringSequenceEqual -Actual @($transport.args) -Expected $gatewayArguments)
) {
    throw "Codex MCP server '$Name' was written, but its command configuration did not match."
}
foreach ($entry in $expectedEnvironment.GetEnumerator()) {
    $property = $transport.env.PSObject.Properties[$entry.Key]
    if ($null -eq $property -or [string]$property.Value -cne [string]$entry.Value) {
        throw "Codex MCP server '$Name' was written, but environment '$($entry.Key)' did not match."
    }
}

Write-Host "Configured Codex MCP server '$Name'."
Write-Host "Control Plane: $normalizedControlPlaneUrl"
Write-Host "Start AITools services through the unified service platform before delegating tasks."
