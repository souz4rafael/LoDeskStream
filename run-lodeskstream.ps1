$ErrorActionPreference = "Stop"

$app = Join-Path $PSScriptRoot "LoDeskStream.exe"
if (-not (Test-Path $app)) {
    throw "LoDeskStream.exe was not found next to this script."
}

if (-not $env:STREAM_TOKEN) {
    $bytes = New-Object byte[] 18
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    $env:STREAM_TOKEN = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Write-Host "Generated session token (keep private): $env:STREAM_TOKEN"
}

Write-Host "LoDeskStream starting..."
Write-Host "Open http://localhost:8765 or http://<this-PC-IP>:8765"
& $app
exit $LASTEXITCODE
