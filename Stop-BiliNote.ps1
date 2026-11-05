$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateFile = Join-Path $AppRoot '.runtime\server.json'
if (-not (Test-Path -LiteralPath $StateFile)) { Write-Output 'BiliNote is already stopped.'; exit 0 }
$Saved = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
if ($Saved.root -ne $AppRoot) { throw 'The saved application path does not match.' }
$ControlTokenFile = Join-Path $AppRoot 'backend\.local-stop-token'
if (Test-Path -LiteralPath $ControlTokenFile) {
    $ControlToken = (Get-Content -LiteralPath $ControlTokenFile -Raw).Trim()
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:3015/local-control/stop' -Method Post -Headers @{ 'X-Local-Control-Token' = $ControlToken } -TimeoutSec 5
        for ($Attempt = 0; $Attempt -lt 15; $Attempt++) {
            Start-Sleep -Seconds 1
            try { $null = Invoke-RestMethod 'http://127.0.0.1:3015/local-health' -TimeoutSec 1 }
            catch { Remove-Item -LiteralPath $StateFile -Force; Write-Output 'BiliNote stopped.'; exit 0 }
        }
    } catch { }
}
$ServerProcess = Get-Process -Id $Saved.pid -ErrorAction SilentlyContinue
if ($ServerProcess) {
    $ProcessPath = $ServerProcess.Path
    if (-not $ProcessPath -or -not $ProcessPath.StartsWith($AppRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $ServerProcess.ProcessName -notlike 'python*') { throw 'The saved process does not belong to this BiliNote installation.' }
    & taskkill.exe /PID $ServerProcess.Id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'BiliNote could not be stopped.' }
}
Remove-Item -LiteralPath $StateFile -Force
Write-Output 'BiliNote stopped.'
