param([int[]]$WaitForProcessIds = @(), [switch]$ResumeBuild, [switch]$ResumeIndex)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUTF8 = '1'
$taskPython = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$taskStatus = Join-Path (Get-Location) 'reports\full-build-stage.json'
function Set-BuildStage([string]$Stage) {
    @{stage=$Stage; time=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $taskStatus -Encoding utf8
}
function Run-Stage([string]$Stage,[string[]]$Arguments) {
    Set-BuildStage $Stage
    & $taskPython @Arguments 1> (Join-Path 'reports' ($Stage + '.log')) 2> (Join-Path 'reports' ($Stage + '-error.log'))
    if ($LASTEXITCODE -ne 0) { throw "Stage $Stage failed: $LASTEXITCODE. See reports/$Stage.log" }
}
try {
    Set-BuildStage 'waiting-for-sync'
    foreach ($taskProcessId in $WaitForProcessIds) {
        while (Get-Process -Id $taskProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 10 }
    }
    if (-not ($ResumeBuild -or $ResumeIndex)) {
        # Inventory errors remain documented; inaccessible directories cannot be copied.
        Set-BuildStage 'refresh-inventory'
        & $taskPython -m frameseek.cli inventory *> reports\full-inventory.log
        if ($LASTEXITCODE -notin @(0,2)) { throw 'Inventory failed' }
        Run-Stage 'full-sync-verify' @('-m','frameseek.cli','sync')
    }
    if ($ResumeIndex) {
        $taskPreviousScan = Get-Content -LiteralPath 'reports/full-scan.log' -Encoding utf8 |
            Where-Object { $_ -match '^\{"observed"' } | Select-Object -Last 1 | ConvertFrom-Json
        if (-not $taskPreviousScan -or $taskPreviousScan.scan_errors -ne 0) {
            throw 'Index-only resume requires a completed local scan without errors'
        }
    } else {
        Run-Stage 'full-scan' @('-m','frameseek.cli','scan','--trust-stable')
    }
    Run-Stage 'full-index' @('-m','frameseek.cli','index','--retry')
    Run-Stage 'full-vector-verify' @('-m','frameseek.cli','index','--verify-only')
    Run-Stage 'full-status' @('-m','frameseek.cli','status')
    $taskFinalStats = Get-Content -LiteralPath 'reports/full-status.log' -Raw -Encoding utf8 | ConvertFrom-Json
    $taskScopeNames = @($taskFinalStats.scope.directories.PSObject.Properties.Name)
    if ($taskScopeNames.Count -gt 0) {
        if ($taskFinalStats.scope.pending_files -gt 0) { throw 'Unfinished selected-directory tasks remain' }
        Set-BuildStage 'selected-index-built-awaiting-acceptance'
    } else {
        if ($taskFinalStats.pending -gt 0) { throw 'Unfinished index tasks remain; full acceptance cannot begin' }
        Set-BuildStage 'index-built-awaiting-full-acceptance'
    }
} catch {
    @{stage='error'; time=(Get-Date).ToString('o'); error=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath $taskStatus -Encoding utf8
    exit 1
}
