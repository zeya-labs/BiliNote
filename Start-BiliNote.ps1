[CmdletBinding()]
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $AppRoot '.runtime'
$LogDirectory = Join-Path $Runtime 'logs'
$StateFile = Join-Path $Runtime 'server.json'
$AppUrl = 'http://127.0.0.1:3015'
$Python = Join-Path $AppRoot 'backend\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

function Test-LocalServer {
    try {
        $Status = Invoke-RestMethod "$AppUrl/local-health" -TimeoutSec 2
        return $Status.app -eq 'BiliNote' -and $Status.deployment -eq 'local-production'
    } catch { return $false }
}

if (-not (Test-LocalServer)) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'BiliNote Python environment is missing.' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:BACKEND_HOST = '127.0.0.1'
    $env:BACKEND_PORT = '3015'
    $env:HF_HOME = Join-Path $AppRoot '.runtime\huggingface'
    $env:MODELSCOPE_CACHE = Join-Path $AppRoot '.runtime\modelscope'
    $ServerProcess = Start-Process -FilePath $Python -ArgumentList 'local_server.py' -WorkingDirectory (Join-Path $AppRoot 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDirectory 'server.stdout.log') -RedirectStandardError (Join-Path $LogDirectory 'server.stderr.log')
    @{ pid = $ServerProcess.Id; root = $AppRoot; started = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 90; $Attempt++) {
        if (Test-LocalServer) { $Ready = $true; break }
        if ($ServerProcess.HasExited) { throw "BiliNote failed to start. Check $LogDirectory" }
        Start-Sleep -Seconds 1
    }
    if (-not $Ready) { throw "BiliNote startup timed out. Check $LogDirectory" }
}
if (-not $NoBrowser) { Start-Process $AppUrl | Out-Null }
Write-Output "BiliNote is ready at $AppUrl"
