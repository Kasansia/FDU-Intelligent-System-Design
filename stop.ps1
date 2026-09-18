param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Invoke-RestMethod -Method Post "http://127.0.0.1:$Port/api/shutdown" | Out-Null
Write-Host 'Camera service stopped.'
