[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $root ".tmp\multi-agent-service-web-runtime"
$manifestPath = Join-Path $runtimeRoot "service.json"

function Stop-ProcessTree([int]$ProcessId) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $taskkillOptions = @{
        FilePath = Join-Path $env:SystemRoot "System32\taskkill.exe"
        ArgumentList = @('/PID', "$ProcessId", '/T', '/F')
        WindowStyle = 'Hidden'
        Wait = $true
        PassThru = $true
    }
    $taskkill = Start-Process @taskkillOptions
    if ($taskkill.ExitCode -ne 0 -or (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "No managed Service Web process was recorded."
    return
}

$entry = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
if ($process) {
    $actualStart = [DateTimeOffset]$process.StartTime
    $differenceMilliseconds = [Math]::Abs(
        $actualStart.ToUnixTimeMilliseconds() - [long]$entry.startTimeUnixMilliseconds
    )
    if ($differenceMilliseconds -gt 2000) {
        throw "Recorded PID now belongs to a different process; it was not stopped."
    }
    Stop-ProcessTree -ProcessId ([int]$entry.pid)
    if (Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue) {
        throw "The recorded Service Web process could not be stopped."
    }
}
Remove-Item -LiteralPath $manifestPath -Force

Write-Host "Stopped the managed Service Web process."
