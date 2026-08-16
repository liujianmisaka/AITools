[CmdletBinding()]
param(
    [switch]$RunInfrastructure
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$core = Join-Path $root "multi-agent-v2"
$web = Join-Path $root "multi-agent-web-v2"
$frontend = Join-Path $web "frontend"
$acceptance = Join-Path $root ".multi-agent-dev\v2\acceptance"

New-Item -ItemType Directory -Force -Path $acceptance | Out-Null
$env:UV_CACHE_DIR = Join-Path $root ".multi-agent-dev\uv-cache"

function Invoke-Checked {
    param([string]$Label, [scriptblock]$Command)
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Invoke-Checked "Core lock" { uv lock --project $core --check }
Invoke-Checked "Web lock" { uv lock --project $web --check }
Invoke-Checked "Core tests" { uv run --project $core pytest $core }
Invoke-Checked "Web tests" { uv run --project $web pytest $web }
Invoke-Checked "Core lint" { uv run --project $core ruff check $core }
Invoke-Checked "Web lint" { uv run --project $web ruff check $web }
Invoke-Checked "Core types" { uv run --project $core basedpyright }
Invoke-Checked "Web types" { uv run --project $web basedpyright }
Invoke-Checked "Frontend dependencies" { npm ci --prefix $frontend --cache (Join-Path $acceptance "npm-cache") }
Invoke-Checked "Frontend tests" { npm --prefix $frontend test -- --run }
Invoke-Checked "Frontend build" { npm --prefix $frontend run build }
Invoke-Checked "Python SBOM" {
    uv export --project $core --format cyclonedx1.5 --output-file (Join-Path $acceptance "core-sbom.cdx.json")
}
Invoke-Checked "Web Python SBOM" {
    uv export --project $web --format cyclonedx1.5 --output-file (Join-Path $acceptance "web-sbom.cdx.json")
}
Invoke-Checked "Frontend SBOM" {
    npm sbom --prefix $frontend --sbom-format cyclonedx | Set-Content -LiteralPath (Join-Path $acceptance "frontend-sbom.cdx.json") -Encoding utf8
}
Invoke-Checked "Secret and cutover checks" {
    uv run --project $core python (Join-Path $core "tools\phase6_acceptance.py") --repository $root
}

if ($RunInfrastructure) {
    $env:MULTI_AGENT_V2_RUN_INFRA_TESTS = "1"
    Invoke-Checked "PostgreSQL and Temporal integration" {
        uv run --project $core pytest -m integration $core
    }
}

Write-Host "Multi-Agent V2 acceptance completed." -ForegroundColor Green
