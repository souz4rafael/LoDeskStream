[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$SetupExe)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$SetupExe = (Resolve-Path -LiteralPath $SetupExe).Path
$Root = Join-Path $env:TEMP ("LoDeskStream-install-integration-" + [Guid]::NewGuid().ToString('N'))
$Install = Join-Path $Root ("App " + [char]0xE3 + " with space and 'quote")
$Other = Join-Path $Root 'unrelated'
$Passed = $false

function Get-ExternalMetadataSnapshot {
    $paths = @(
        (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'LoDeskStream.lnk'),
        (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\LoDeskStream\LoDeskStream.lnk')
    )
    $result = [ordered]@{}
    foreach ($path in $paths) {
        $result[$path] = if (Test-Path -LiteralPath $path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash } else { $null }
    }
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream'
    if (Test-Path -LiteralPath $key) {
        $item = Get-Item -LiteralPath $key
        $values = [ordered]@{}
        foreach ($name in ($item.GetValueNames() | Sort-Object)) { $values[$name] = $item.GetValue($name) }
        $result['registry'] = $values | ConvertTo-Json -Compress -Depth 10
    } else { $result['registry'] = $null }
    return ($result | ConvertTo-Json -Compress -Depth 10)
}

function Invoke-Setup {
    param([string[]]$Arguments, [bool]$ExpectSuccess)
    & $SetupExe @Arguments | Out-Host
    $exitCode = $LASTEXITCODE
    if (($exitCode -eq 0) -ne $ExpectSuccess) {
        throw "Unexpected setup result $exitCode for $($Arguments -join ' ')"
    }
}

if (Test-Path -LiteralPath $Root) { throw 'Integration directory must be fresh.' }
New-Item -ItemType Directory -Path $Other | Out-Null
$Before = Get-ExternalMetadataSnapshot
try {
    Set-Content -LiteralPath (Join-Path $Other 'keep.txt') -Value 'unrelated data'
    Invoke-Setup -Arguments @('--install-dir', $Other, '--no-shortcuts', '--no-registration', '--quiet') -ExpectSuccess $false
    if ((Get-Content -LiteralPath (Join-Path $Other 'keep.txt') -Raw).Trim() -ne 'unrelated data') {
        throw 'Unrelated folder was modified.'
    }
    Invoke-Setup -Arguments @('--install-dir', $Install, '--no-shortcuts', '--no-registration', '--quiet') -ExpectSuccess $true
    $marker = Get-Content -LiteralPath (Join-Path $Install '.lodeskstream-install.json') -Raw | ConvertFrom-Json
    if ($marker.Application -ne 'LoDeskStream' -or $marker.Shortcuts -or $marker.Registration) {
        throw 'Unexpected installation marker.'
    }
    foreach ($required in @('LoDeskStream.exe', 'manager\LoDeskStreamManager.exe', '_internal\build-info.json', 'uninstall-lodeskstream.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Install $required))) { throw "Missing installed file: $required" }
    }
    Set-Content -LiteralPath (Join-Path $Install 'obsolete.txt') -Value 'old installation fixture'
    Invoke-Setup -Arguments @('--install-dir', $Install, '--no-shortcuts', '--no-registration', '--quiet') -ExpectSuccess $true
    if (Test-Path -LiteralPath (Join-Path $Install 'obsolete.txt')) { throw 'Update retained an obsolete payload file.' }
    Invoke-Setup -Arguments @('--uninstall', '--install-dir', $Install, '--quiet') -ExpectSuccess $true
    if (Test-Path -LiteralPath $Install) { throw 'Uninstall left the installation folder.' }
    if ((Get-Content -LiteralPath (Join-Path $Other 'keep.txt') -Raw).Trim() -ne 'unrelated data') {
        throw 'Uninstall affected an unrelated folder.'
    }
    if ((Get-ExternalMetadataSnapshot) -ne $Before) { throw 'Isolated setup changed real shortcuts or uninstall registration.' }
    $Passed = $true
    Write-Host 'Isolated first install, reinstall/update, scoped uninstall and unrelated-folder protection passed.'
    Write-Host 'This does not certify a clean Windows machine or upgrade from an older public release.'
}
finally {
    if ($Passed) { Remove-Item -LiteralPath $Root -Recurse -Force }
    else { Write-Warning "Integration diagnostics retained at: $Root" }
}
