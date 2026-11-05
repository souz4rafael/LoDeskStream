<#
.SYNOPSIS
Builds fresh LOCAL DEVELOPMENT CANDIDATES only; licensing is deferred.
.DESCRIPTION
Requires Python 3.12, requirements-build.txt already installed in the selected
interpreter, .NET SDK 10, Node, and (for Git checkouts) Git. Never installs packages.
Stages outside OneDrive in a unique TEMP directory. Existing dist/installer files
are never consumed or changed. Stable packaging is not bit-for-bit reproducibility.
.PARAMETER SkipTests
Explicitly bypasses validation tests. Outputs remain local development candidates.
#>
[CmdletBinding()]
param(
    [string]$PythonExe,
    [string]$OutputDirectory,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Work = $null
$Promoted = $false

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$Arguments = @()
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Native command failed (exit $LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}

function Resolve-Tool {
    param([string]$Name)
    $Tool = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $Tool) {
        throw "Required tool '$Name' was not found. Install/configure it and rerun; this script never installs dependencies."
    }
    return $Tool.Source
}

try {
    if ($env:OS -ne 'Windows_NT') {
        throw 'Packaging requires Windows (win-x64).'
    }
    if ([string]::IsNullOrWhiteSpace($PythonExe)) {
        $RepoPython = Join-Path $Root '.venv\Scripts\python.exe'
        $PythonExe = if (Test-Path -LiteralPath $RepoPython -PathType Leaf) { $RepoPython } else { 'python' }
    }
    $PythonExe = Resolve-Tool $PythonExe
    $Dotnet = Resolve-Tool 'dotnet'
    $Node = Resolve-Tool 'node'
    $Helper = Join-Path $Root 'tools\build_support.py'
    [xml]$Props = Get-Content -LiteralPath (Join-Path $Root 'Directory.Build.props') -Raw
    $Version = [string]$Props.Project.PropertyGroup[0].Version
    if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version in Directory.Build.props.' }
    $SessionId = [Guid]::NewGuid().ToString('N')
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        $OutputDirectory = Join-Path $Root "local-candidates\$Version-$SessionId"
    }
    $OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
    if (Test-Path -LiteralPath $OutputDirectory) {
        $OutputItem = Get-Item -LiteralPath $OutputDirectory -Force
        if (-not $OutputItem.PSIsContainer -or
            ($OutputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            @(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count -ne 0) {
            throw "OutputDirectory must be absent or empty; refusing overwrite: $OutputDirectory"
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:TEMP)) {
        throw 'TEMP must point to a writable directory outside OneDrive.'
    }
    $TempRoot = [IO.Path]::GetFullPath($env:TEMP)
    if ($TempRoot -match '(?i)(^|\\)OneDrive(?: - [^\\]+)?(\\|$)') {
        throw 'TEMP is inside OneDrive. Set TEMP to a local, non-synced directory and rerun.'
    }
    foreach ($OneDriveRoot in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
        if (-not [string]::IsNullOrWhiteSpace($OneDriveRoot)) {
            $CloudRoot = [IO.Path]::GetFullPath($OneDriveRoot).TrimEnd('\') + '\'
            if (($TempRoot.TrimEnd('\') + '\').StartsWith($CloudRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'TEMP is inside OneDrive. Set TEMP to a local, non-synced directory and rerun.'
            }
        }
    }
    $Work = Join-Path $TempRoot "LoDeskStream-build-$SessionId"
    if (Test-Path -LiteralPath $Work) { throw "Work directory already exists: $Work" }
    New-Item -ItemType Directory -Path $Work | Out-Null
    Write-Host "LOCAL DEVELOPMENT CANDIDATE ONLY - licensing deferred."
    Write-Host "Diagnostic work directory: $Work"
    $DotnetVersion = (Invoke-Native $Dotnet @('--version') | Out-String).Trim()
    $NodeVersion = (Invoke-Native $Node @('--version') | Out-String).Trim()
    if ($DotnetVersion -notmatch '^10\.') {
        throw "The projects require .NET SDK 10; selected SDK is $DotnetVersion."
    }
    $PrepareArgs = @($Helper, 'prepare', '--root', $Root, '--work', $Work,
        '--output', $OutputDirectory, '--dotnet-version', $DotnetVersion, '--node-version', $NodeVersion)
    if (Test-Path -LiteralPath (Join-Path $Root '.git')) {
        $Git = Resolve-Tool 'git'
        $Revision = (Invoke-Native $Git @('-C', $Root, 'rev-parse', 'HEAD') | Out-String).Trim()
        $Status = (Invoke-Native $Git @('-C', $Root, 'status', '--porcelain', '--untracked-files=normal') | Out-String).Trim()
        $Dirty = if ($Status.Length -eq 0) { 'false' } else { 'true' }
        $PrepareArgs += @('--revision', $Revision, '--dirty', $Dirty)
    }
    Invoke-Native $PythonExe $PrepareArgs
    Push-Location $Root
    try {
        if ($SkipTests) {
            Write-Warning 'SkipTests explicitly requested: validation tests are NOT being run.'
        }
        else {
            Invoke-Native $PythonExe @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py')
            $NodeTests = @(Get-ChildItem -LiteralPath (Join-Path $Root 'tests') -Filter '*.test.cjs' -File |
                Sort-Object Name | ForEach-Object { $_.FullName })
            if ($NodeTests.Count -eq 0) { throw 'No tests\*.test.cjs files found.' }
            Invoke-Native $Node (@('--test') + $NodeTests)
            foreach ($TestProject in @('tests\tray\LoDeskStream.TrayTests.csproj',
                                      'tests\installer\LoDeskStream.InstallerTests.csproj')) {
                $ProjectPath = Join-Path $Root $TestProject
                if (-not (Test-Path -LiteralPath $ProjectPath -PathType Leaf)) {
                    throw "Missing validation project: $TestProject. Complete the source tree or explicitly use -SkipTests."
                }
                Invoke-Native $Dotnet @('run', '-c', 'Release', '--project', $ProjectPath,
                    "-p:LoDeskStreamBuildRoot=$Work\tests-dotnet")
            }
        }

        # Never consume a checked-in/ignored spec or an old dist output.
        $PyDist = Join-Path $Work 'pyinstaller-dist'
        $SpecPath = Join-Path $Work 'spec'
        $PyWork = Join-Path $Work 'pyinstaller-work'
        $Icon = Join-Path $Work 'LoDeskStream.ico'
        $BuildInfo = Join-Path $Work 'build-info.json'
        $PreviousPyInstallerCache = $env:PYINSTALLER_CONFIG_DIR
        $env:PYINSTALLER_CONFIG_DIR = Join-Path $Work 'pyinstaller-cache'
        try {
            Invoke-Native $PythonExe @('-m', 'PyInstaller', '--clean', '--onedir', '--console',
                '--name', 'LoDeskStream', '--contents-directory', '_internal',
                '--specpath', $SpecPath, '--workpath', $PyWork, '--distpath', $PyDist,
                '--icon', $Icon, '--add-data', "$(Join-Path $Root 'web');web",
                '--add-data', "$BuildInfo;.", '--collect-all', 'aiortc',
                '--collect-all', 'av', '--collect-all', 'soundcard', (Join-Path $Root 'server.py'))
        }
        finally {
            $env:PYINSTALLER_CONFIG_DIR = $PreviousPyInstallerCache
        }
        $Portable = Join-Path $Work 'portable'
        $ServerOutput = Join-Path $PyDist 'LoDeskStream'
        if (-not (Test-Path -LiteralPath $ServerOutput -PathType Container)) {
            throw 'PyInstaller did not produce the expected fresh server directory.'
        }
        Move-Item -LiteralPath $ServerOutput -Destination $Portable
        Invoke-Native $PythonExe @($Helper, 'check-exe', '--path', (Join-Path $Portable 'LoDeskStream.exe'))

        $ManagerOutput = Join-Path $Portable 'manager'
        Invoke-Native $Dotnet @('publish', (Join-Path $Root 'manager-src\LoDeskStreamManager.csproj'),
            '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '--output', $ManagerOutput,
            "-p:LoDeskStreamBuildRoot=$Work\publish-dotnet", '-p:PublishSingleFile=true',
            '-p:EnableCompressionInSingleFile=true', '-p:IncludeNativeLibrariesForSelfExtract=true')
        Invoke-Native $PythonExe @($Helper, 'check-exe', '--path', (Join-Path $ManagerOutput 'LoDeskStreamManager.exe'))
        foreach ($Name in @('README.md', 'run-lodeskstream.ps1')) {
            $Target = Join-Path $Portable $Name
            if (Test-Path -LiteralPath $Target) { throw "Unexpected preexisting payload file: $Name" }
            Copy-Item -LiteralPath (Join-Path $Root $Name) -Destination $Target
        }
        Invoke-Native $PythonExe @($Helper, 'manifest', '--root', $Portable, '--version', $Version)
        $Final = Join-Path $Work 'final'
        New-Item -ItemType Directory -Path $Final | Out-Null
        $Zip = Join-Path $Final 'LoDeskStream-Windows-portable.zip'
        Invoke-Native $PythonExe @($Helper, 'zip', '--root', $Portable, '--version', $Version, '--archive', $Zip)

        $InstallerOutput = Join-Path $Work 'installer-publish'
        Invoke-Native $Dotnet @('publish', (Join-Path $Root 'installer-src\LoDeskStreamInstaller.csproj'),
            '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '--output', $InstallerOutput,
            "-p:LoDeskStreamBuildRoot=$Work\publish-dotnet", "-p:PayloadZip=$Zip",
            '-p:PublishSingleFile=true', '-p:EnableCompressionInSingleFile=true',
            '-p:IncludeNativeLibrariesForSelfExtract=true')
        $Setup = Join-Path $InstallerOutput 'LoDeskStream-Setup.exe'
        Invoke-Native $PythonExe @($Helper, 'check-exe', '--path', $Setup)
        Copy-Item -LiteralPath $Setup -Destination (Join-Path $Final 'LoDeskStream-Setup.exe')
        Invoke-Native $PythonExe @($Helper, 'verify-source', '--root', $Root, '--build-info', $BuildInfo)
        Invoke-Native $PythonExe @($Helper, 'verify-zip', '--root', $Portable, '--version', $Version, '--archive', $Zip)
        Invoke-Native $PythonExe @($Helper, 'checksums', '--root', $Final)
        Invoke-Native $PythonExe @($Helper, 'verify-final', '--root', $Final)
        Invoke-Native $PythonExe @($Helper, 'promote', '--source', $Final, '--output', $OutputDirectory)
        $Promoted = $true
    }
    finally {
        Pop-Location
    }
    try {
        Remove-Item -LiteralPath $Work -Recurse -Force
    }
    catch {
        Write-Warning "Candidate is complete, but diagnostic work cleanup failed: $Work"
    }
    Write-Host "Complete LOCAL DEVELOPMENT CANDIDATE (not distribution-ready): $OutputDirectory"
    Write-Host 'Artifacts: LoDeskStream-Windows-portable.zip, LoDeskStream-Setup.exe, SHA256SUMS.txt'
}
catch {
    if (-not $Promoted -and $null -ne $Work -and (Test-Path -LiteralPath $Work)) {
        Write-Warning "Build failed; no candidate promoted. Diagnostic work retained at: $Work"
    }
    throw
}
