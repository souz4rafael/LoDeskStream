$ErrorActionPreference = "Stop"

$app = Join-Path $PSScriptRoot "LoDeskStream.exe"
if (-not (Test-Path $app)) {
    throw "LoDeskStream.exe was not found next to this script."
}

if (-not $env:STREAM_TOKEN) {
    $env:STREAM_TOKEN = [Convert]::ToBase64String([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(18)).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

Write-Host "LoDeskStream starting..."
Write-Host "STREAM_TOKEN: $env:STREAM_TOKEN"
Write-Host "Open http://localhost:8765 or http://<this-PC-IP>:8765"
& $app
