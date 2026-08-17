[CmdletBinding()]
param(
    [switch]$RunInfrastructure,
    [switch]$RunCapacity
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$core = Join-Path $root "multi-agent-v2"
$web = Join-Path $root "multi-agent-web-v2"
$frontend = Join-Path $web "frontend"
$acceptance = Join-Path $root ".multi-agent-dev\v2\acceptance"
$frontendBoundary = [IO.Path]::GetFullPath($frontend).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
$generatedPaths = @(
    (Join-Path $frontend "node_modules"),
    (Join-Path $frontend "dist"),
    (Join-Path $frontend "tsconfig.app.tsbuildinfo"),
    (Join-Path $frontend "tsconfig.node.tsbuildinfo")
)
$preexistingPaths = @{}
foreach ($path in $generatedPaths) {
    $preexistingPaths[$path] = Test-Path -LiteralPath $path
}
$environmentVariableNames = @(
    "UV_CACHE_DIR",
    "MULTI_AGENT_V2_RUN_INFRA_TESTS",
    "MULTI_AGENT_V2_RUN_CAPACITY_TESTS"
)
$previousEnvironment = @{}
foreach ($name in $environmentVariableNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        [EnvironmentVariableTarget]::Process
    )
}

New-Item -ItemType Directory -Force -Path $acceptance | Out-Null
$env:UV_CACHE_DIR = Join-Path $root ".multi-agent-dev\uv-cache"
$env:MULTI_AGENT_V2_RUN_INFRA_TESTS = "0"
$env:MULTI_AGENT_V2_RUN_CAPACITY_TESTS = "0"

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Command)
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

try {
    Invoke-Checked "Core lock" { uv lock --project $core --check }
    Invoke-Checked "Web lock" { uv lock --project $web --check }
    Invoke-Checked "Core tests" { uv run --project $core pytest $core }
    Invoke-Checked "Web tests" { uv run --project $web pytest $web }
    Invoke-Checked "Core lint" { uv run --project $core ruff check $core }
    Invoke-Checked "Web lint" { uv run --project $web ruff check $web }
    Invoke-Checked "Core types" {
        uv run --project $core basedpyright --project (Join-Path $core "pyproject.toml")
    }
    Invoke-Checked "Web types" {
        uv run --project $web basedpyright --project (Join-Path $web "pyproject.toml")
    }
    Invoke-Checked "Frontend dependencies" {
        npm ci --prefix $frontend --cache (Join-Path $acceptance "npm-cache")
    }
    Invoke-Checked "Frontend tests" { npm --prefix $frontend test -- --run }
    Invoke-Checked "Frontend build" { npm --prefix $frontend run build }
    Invoke-Checked "Python SBOM" {
        uv export --project $core --preview-features sbom-export --format cyclonedx1.5 --output-file (Join-Path $acceptance "core-sbom.cdx.json") |
            Out-Null
    }
    Invoke-Checked "Web Python SBOM" {
        uv export --project $web --preview-features sbom-export --format cyclonedx1.5 --output-file (Join-Path $acceptance "web-sbom.cdx.json") |
            Out-Null
    }
    Invoke-Checked "Frontend SBOM" {
        npm sbom --prefix $frontend --sbom-format cyclonedx |
            Set-Content -LiteralPath (Join-Path $acceptance "frontend-sbom.cdx.json") -Encoding utf8
    }
    Invoke-Checked "Secret and cutover checks" {
        uv run --project $core python (Join-Path $core "tools\phase6_acceptance.py") --repository $root
    }
    Invoke-Checked "Installed console entrypoints" {
        uv run --project $core python (Join-Path $core "tools\phase7_acceptance.py") --project $core
    }

    if ($RunInfrastructure) {
        $env:MULTI_AGENT_V2_RUN_INFRA_TESTS = "1"
        Invoke-Checked "PostgreSQL and Temporal integration" {
            uv run --project $core pytest -m integration $core
        }
    }

    if ($RunCapacity) {
        $env:MULTI_AGENT_V2_RUN_CAPACITY_TESTS = "1"
        Invoke-Checked "1000 waiting workflow capacity" {
            uv run --project $core pytest -m capacity $core
        }
    }

    Write-Host "Multi-Agent V2 acceptance completed." -ForegroundColor Green
} finally {
    try {
        foreach ($path in $generatedPaths) {
            if (-not $preexistingPaths[$path] -and (Test-Path -LiteralPath $path)) {
                $resolved = [IO.Path]::GetFullPath($path)
                if (-not $resolved.StartsWith(
                    $frontendBoundary,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    throw "Refusing to clean generated path outside frontend: $resolved"
                }
                $item = Get-Item -LiteralPath $resolved -Force
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw "Refusing to clean generated reparse point: $resolved"
                }
                Remove-Item -LiteralPath $resolved -Recurse -Force
            }
        }
    } finally {
        foreach ($name in $environmentVariableNames) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $previousEnvironment[$name],
                [EnvironmentVariableTarget]::Process
            )
        }
    }
}
