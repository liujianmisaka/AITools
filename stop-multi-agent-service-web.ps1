[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeRoot = Join-Path $root ".tmp\multi-agent-service-web-runtime"
$manifestPath = Join-Path $runtimeRoot "services.json"

function Get-ProcessStartUnixMilliseconds([System.Diagnostics.Process]$Process) {
    return ([DateTimeOffset]$Process.StartTime).ToUnixTimeMilliseconds()
}

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

function Stop-RecordedProcess([object]$Entry) {
    $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    $differenceMilliseconds = [Math]::Abs(
        (Get-ProcessStartUnixMilliseconds -Process $process) -
        [long]$Entry.startTimeUnixMilliseconds
    )
    if ($differenceMilliseconds -gt 2000) {
        throw "Recorded PID $($Entry.pid) now belongs to a different process."
    }
    Stop-ProcessTree -ProcessId ([int]$Entry.pid)
    if (Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue) {
        throw "Recorded process $($Entry.pid) could not be stopped."
    }
}

if (-not (Test-Path -LiteralPath $manifestPath)) {
    Write-Host "No managed AITools Service Web runtime was recorded."
    return
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.managementUrl) {
    try {
        Invoke-RestMethod -Method Post "$($manifest.managementUrl)/groups/all/stop" -TimeoutSec 30 | Out-Null
    } catch {
        Write-Warning "The Management API did not complete graceful service shutdown; process-tree cleanup will continue."
    }
}

foreach ($entry in @($manifest.services | Sort-Object role -Descending)) {
    Stop-RecordedProcess -Entry $entry
}
Remove-Item -LiteralPath $manifestPath -Force

Write-Host "Stopped the AITools management UI, Management API, and managed service processes."
