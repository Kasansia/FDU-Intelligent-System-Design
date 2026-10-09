param([int]$Camera = -1, [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run setup.ps1 first.' }
try {
    $status = Invoke-RestMethod "http://127.0.0.1:$Port/api/landmarks" -TimeoutSec 2
    if ($status.schema_version -in @(1, 2) -or $status.status -eq 'starting') {
        Start-Process "http://127.0.0.1:$Port"
        Write-Host "Already running: http://127.0.0.1:$Port"
        exit
    }
} catch { }
New-Item -ItemType Directory -Force (Join-Path $projectRoot 'outputs') | Out-Null
$appArgs = @(('"' + (Join-Path $projectRoot 'app.py') + '"'), '--port', "$Port")
if ($Camera -ge 0) { $appArgs += @('--camera', $Camera) }
$proc = Start-Process -FilePath $pythonPath -ArgumentList $appArgs -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'outputs\server.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'outputs\server.stderr.log')
Set-Content -LiteralPath (Join-Path $projectRoot 'outputs\server.pid') -Value $proc.Id
Start-Process "http://127.0.0.1:$Port"
Write-Host "Started PID $($proc.Id): http://127.0.0.1:$Port"
