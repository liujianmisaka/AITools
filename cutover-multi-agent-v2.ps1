[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ArchiveRoot = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ArchiveRoot) {
    $ArchiveRoot = Join-Path $root ".multi-agent-dev\v1-archive"
}
$archive = Join-Path ([IO.Path]::GetFullPath($ArchiveRoot)) ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))
$legacyManifest = Join-Path $root ".multi-agent-dev\processes.json"
$legacyDatabases = @(
    (Join-Path $root "multi-agent\data\state.sqlite3"),
    (Join-Path $root ".multi-agent-dev\state.sqlite3")
)

if (Test-Path -LiteralPath $legacyManifest) {
    throw "A V1 process manifest still exists. Stop V1 before cutover: $legacyManifest"
}

$existing = @($legacyDatabases | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -eq 0) {
    Write-Host "No V1 SQLite database was found; no archive is required."
    return
}

if ($PSCmdlet.ShouldProcess(($existing -join ", "), "Archive V1 SQLite databases to $archive")) {
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    foreach ($database in $existing) {
        $name = Split-Path -Leaf (Split-Path -Parent $database)
        Copy-Item -LiteralPath $database -Destination (Join-Path $archive "$name-state.sqlite3")
    }
    Get-ChildItem -LiteralPath $archive -Filter "*.sqlite3" -File |
        Get-FileHash -Algorithm SHA256 |
        ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $archive "sha256.json") -Encoding utf8
    Write-Host "Archived V1 SQLite data to $archive" -ForegroundColor Green
}
