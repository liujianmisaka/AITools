[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "Low")]
param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$Name = "multi_agent_coordinator",
    [ValidateNotNullOrEmpty()]
    [string]$CoordinatorMcpUrl = "http://127.0.0.1:8020/mcp",
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

$parsedUrl = $null
$trimmedUrl = $CoordinatorMcpUrl.Trim()
$isAbsoluteUrl = [Uri]::TryCreate($trimmedUrl, [UriKind]::Absolute, [ref]$parsedUrl)
if (
    -not $isAbsoluteUrl -or
    $parsedUrl.Scheme -notin @("http", "https") -or
    -not $parsedUrl.Host -or
    $parsedUrl.UserInfo -or
    $parsedUrl.Query -or
    $parsedUrl.Fragment
) {
    throw "CoordinatorMcpUrl must be an absolute HTTP(S) URL without credentials, query, or fragment."
}
$normalizedUrl = $trimmedUrl.TrimEnd("/")

$codex = Get-Command -Name $CodexCommand -ErrorAction Stop | Select-Object -First 1
$codexExecutable = if ($codex.Source) { $codex.Source } else { $codex.Definition }
if (-not $PSCmdlet.ShouldProcess(
    "global Codex MCP server '$Name'",
    "register the managed Multi-Agent Coordinator endpoint"
)) {
    return
}

$addOutput = Invoke-CodexCommand -Executable $codexExecutable -ArgumentList @(
    "mcp", "add", $Name, "--url", $normalizedUrl
)
if ($addOutput) {
    Write-Host $addOutput
}

$configurationJson = Invoke-CodexCommand -Executable $codexExecutable -ArgumentList @(
    "mcp", "get", $Name, "--json"
)
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
    $transport.type -ne "streamable_http" -or
    $null -eq $transport.url -or
    $transport.url.TrimEnd("/") -ne $normalizedUrl
) {
    throw "Codex MCP server '$Name' was written, but its HTTP configuration did not match."
}

Write-Host "Configured Codex MCP server '$Name'."
Write-Host "Coordinator MCP: $normalizedUrl"
Write-Host "Start the core services through AITools Service Web before using Coordinator tools."
