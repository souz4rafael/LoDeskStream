using System.ComponentModel;
using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;
using System.Text.Json;
using System.Text;
using LoDeskStream.Installation;
using Microsoft.Win32;

try
{
    return Setup.Run(args);
}
catch (Exception exc) when (exc is IOException or InvalidDataException or UnauthorizedAccessException or ArgumentException
    or InvalidOperationException or Win32Exception or COMException or SecurityException
    or JsonException or TimeoutException or AggregateException)
{
    Console.Error.WriteLine($"LoDeskStream setup failed: {exc.Message}");
    return 1;
}

internal static class Setup
{
    private const string AppName = "LoDeskStream";
    private const string MarkerName = ".lodeskstream-install.json";
    private const string RegistryPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream";
    private static readonly JsonSerializerOptions JsonOptions = new() { PropertyNameCaseInsensitive = true };
    private static readonly string DefaultInstall = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", AppName);
    private static string Version => Assembly.GetExecutingAssembly().GetName().Version?.ToString(3)
        ?? throw new InvalidOperationException("Missing installer version.");

    public static int Run(string[] args)
    {
        var quiet = false;
        var shortcuts = true;
        var registration = true;
        var uninstall = false;
        var target = DefaultInstall;
        for (var index = 0; index < args.Length; index++)
        {
            switch (args[index])
            {
                case "--quiet": quiet = true; break;
                case "--no-shortcuts": shortcuts = false; break;
                case "--no-registration": registration = false; break;
                case "--uninstall": uninstall = true; break;
                case "--install-dir":
                    if (++index >= args.Length || !Path.IsPathFullyQualified(args[index]))
                        throw new ArgumentException("--install-dir requires an absolute folder path.");
                    target = args[index];
                    break;
                case "--help":
                    Console.WriteLine("LoDeskStream Setup [--quiet] [--install-dir <absolute path>] [--no-shortcuts] [--no-registration] [--uninstall]");
                    return 0;
                default: throw new ArgumentException($"Unknown argument: {args[index]}");
            }
        }
        target = Path.TrimEndingDirectorySeparator(Path.GetFullPath(target));
        if (string.IsNullOrEmpty(Path.GetFileName(target)))
            throw new ArgumentException("A drive root cannot be an installation folder.");
        if (uninstall)
        {
            ValidateMarker(target);
            RunPowerShellFile(Path.Combine(target, "uninstall-lodeskstream.ps1"), quiet);
            return 0;
        }
        var identity = WindowsIdentity.GetCurrent().User?.Value
            ?? throw new InvalidOperationException("Could not identify the installing user.");
        using var setupLock = new Mutex(true, $@"Local\LoDeskStream.Setup.{identity}", out var lockCreated);
        if (!lockCreated) throw new InvalidOperationException("Another LoDeskStream setup operation is running.");

        ValidateExistingTarget(target);
        if (registration)
        {
            using var existing = Registry.CurrentUser.OpenSubKey(RegistryPath);
            if (existing?.GetValue("InstallLocation") is string location &&
                !SamePath(location, target))
                throw new InvalidOperationException("Another installation is registered. Uninstall it before changing the install location.");
        }
        var parent = Path.GetDirectoryName(target) ?? throw new ArgumentException("Invalid install folder.");
        Directory.CreateDirectory(parent);
        var staging = Path.Combine(parent, $".LoDeskStream-staging-{Guid.NewGuid():N}");
        Directory.CreateDirectory(staging);
        try
        {
            using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("LoDeskStream-Windows-portable.zip")
                ?? throw new InvalidDataException("Missing embedded payload."))
            using (var archive = new ZipArchive(payload, ZipArchiveMode.Read))
                archive.ExtractToDirectory(staging);
            PayloadValidator.Validate(staging, Version);
            Console.WriteLine($"LoDeskStream {Version} development candidate");
            Console.WriteLine("Distribution licensing remains pending. Installing locally only.");
            Console.WriteLine($"Installing to: {target}");

            var metadata = new MetadataSnapshot(shortcuts, registration);
            var restartManager = StopInstalledProcesses(target);
            var marker = new InstallMarker(AppName, Version, target, shortcuts, registration);
            File.WriteAllText(Path.Combine(staging, MarkerName), JsonSerializer.Serialize(marker));
            File.WriteAllText(Path.Combine(staging, "uninstall-lodeskstream.ps1"),
                UninstallScript(target), new UTF8Encoding(encoderShouldEmitUTF8Identifier: true));
            try
            {
                InstallTransaction.Execute(target, staging, () =>
                {
                    if (shortcuts)
                        foreach (var shortcut in ShortcutPaths()) CreateShortcut(shortcut, target);
                    if (registration) RegisterUninstaller(target);
                }, metadata.Restore);
            }
            catch (Exception original) when (original is not AggregateException)
            {
                if (restartManager && Directory.Exists(target))
                {
                    try { StartManager(target); }
                    catch (Exception restartError) when (restartError is Win32Exception or InvalidOperationException)
                    {
                        throw new AggregateException("Update failed; files restored, but the tray restart also failed.", original, restartError);
                    }
                }
                throw;
            }
            if (restartManager) StartManager(target);
            Console.WriteLine("LoDeskStream installed successfully.");
            if (!quiet && !Console.IsInputRedirected)
            {
                Console.WriteLine("Press any key to close.");
                Console.ReadKey(intercept: true);
            }
            return 0;
        }
        finally { InstallTransaction.Cleanup(staging); }
    }

    private static bool SamePath(string first, string second) =>
        string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(first)),
            Path.TrimEndingDirectorySeparator(Path.GetFullPath(second)), StringComparison.OrdinalIgnoreCase);

    private static void ValidateExistingTarget(string target)
    {
        if (!Directory.Exists(target)) return;
        if ((File.GetAttributes(target) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("Installation through a linked folder is not supported.");
        if (!Directory.EnumerateFileSystemEntries(target).Any()) return;
        if (File.Exists(Path.Combine(target, MarkerName))) { ValidateMarker(target); return; }
        using var key = Registry.CurrentUser.OpenSubKey(RegistryPath);
        if (SamePath(target, DefaultInstall) && key?.GetValue("InstallLocation") is string location &&
            SamePath(location, target) && File.Exists(Path.Combine(target, "LoDeskStream.exe")) &&
            File.Exists(Path.Combine(target, "uninstall-lodeskstream.ps1")))
            return;
        throw new InvalidOperationException("Refusing to replace a non-empty folder that is not a recognized LoDeskStream installation.");
    }

    private static void ValidateMarker(string target)
    {
        var marker = JsonSerializer.Deserialize<InstallMarker>(
            File.ReadAllText(Path.Combine(target, MarkerName)), JsonOptions);
        if (marker is null || marker.Application != AppName || !SamePath(marker.InstallDir, target))
            throw new InvalidDataException("Invalid installation marker.");
        if ((File.GetAttributes(target) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidOperationException("Linked installation folders are not supported.");
    }

    private static string[] ShortcutPaths() => [
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "LoDeskStream.lnk"),
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", AppName, "LoDeskStream.lnk")
    ];

    private static void CreateShortcut(string path, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var script = $"$shell=New-Object -ComObject WScript.Shell; $link=$shell.CreateShortcut('{Escape(path)}'); " +
            $"$link.TargetPath='{Escape(Path.Combine(target, "manager", "LoDeskStreamManager.exe"))}'; " +
            $"$link.WorkingDirectory='{Escape(target)}'; $link.IconLocation='{Escape(Path.Combine(target, "LoDeskStream.exe"))}'; $link.Save();";
        RunPowerShellCommand(script);
    }

    private static string Escape(string value) => value.Replace("'", "''");

    private static void RunPowerShellCommand(string script)
    {
        var info = new ProcessStartInfo("powershell.exe") { UseShellExecute = false, CreateNoWindow = true };
        foreach (var value in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script })
            info.ArgumentList.Add(value);
        RunProcess(info);
    }

    private static void RunPowerShellFile(string path, bool quiet)
    {
        var info = new ProcessStartInfo("powershell.exe") {
            UseShellExecute = false, WorkingDirectory = Path.GetTempPath()
        };
        foreach (var value in new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path })
            info.ArgumentList.Add(value);
        if (quiet) info.ArgumentList.Add("-Quiet");
        RunProcess(info);
    }

    private static void RunProcess(ProcessStartInfo info)
    {
        using var process = Process.Start(info) ?? throw new InvalidOperationException("Could not start setup helper.");
        process.WaitForExit();
        if (process.ExitCode != 0) throw new InvalidOperationException($"Setup helper failed with exit code {process.ExitCode}.");
    }

    private static bool StopInstalledProcesses(string target)
    {
        var server = Path.Combine(target, "LoDeskStream.exe");
        var manager = Path.Combine(target, "manager", "LoDeskStreamManager.exe");
        var restart = false;
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                try
                {
                    if (process.ProcessName is not ("LoDeskStream" or "LoDeskStreamManager")) continue;
                    var path = process.MainModule?.FileName;
                    if (path is null || (!SamePath(path, server) && !SamePath(path, manager))) continue;
                    restart |= SamePath(path, manager);
                    process.Kill(entireProcessTree: true);
                    if (!process.WaitForExit(10000)) throw new TimeoutException("Installed application did not stop.");
                }
                catch (InvalidOperationException) when (process.HasExited) { }
            }
        }
        return restart;
    }

    private static void StartManager(string target)
    {
        using var process = Process.Start(new ProcessStartInfo(
            Path.Combine(target, "manager", "LoDeskStreamManager.exe")) {
            UseShellExecute = true, WorkingDirectory = target
        }) ?? throw new InvalidOperationException("Installation restored, but the tray could not be restarted.");
    }

    private static void RegisterUninstaller(string target)
    {
        using var key = Registry.CurrentUser.CreateSubKey(RegistryPath, writable: true)
            ?? throw new InvalidOperationException("Could not register uninstaller.");
        key.SetValue("DisplayName", AppName);
        key.SetValue("DisplayVersion", Version);
        key.SetValue("Publisher", "souz4rafael");
        key.SetValue("InstallLocation", target);
        key.SetValue("DisplayIcon", Path.Combine(target, "LoDeskStream.exe"));
        var command = $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{Path.Combine(target, "uninstall-lodeskstream.ps1")}\"";
        key.SetValue("UninstallString", command);
        key.SetValue("QuietUninstallString", command + " -Quiet");
        key.SetValue("NoModify", 1, RegistryValueKind.DWord);
        key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
    }

    private static string UninstallScript(string target) => $$"""
param([switch]$Quiet)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$created = $false
$setupLock = [Threading.Mutex]::new($true, "Local\LoDeskStream.Setup.$identity", [ref]$created)
if (-not $created) {
    $setupLock.Dispose()
    throw 'Another LoDeskStream setup operation is running.'
}
try {
$installDir = '{{Escape(target)}}'
$markerPath = Join-Path $installDir '.lodeskstream-install.json'
$marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
if ($marker.Application -ne 'LoDeskStream' -or
    -not [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($marker.InstallDir), [IO.Path]::GetFullPath($installDir))) {
    throw 'Invalid installation marker; uninstall cancelled.'
}
if ((Get-Item -LiteralPath $installDir).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Linked installation folders are not supported.'
}
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream'
if ($marker.Registration -and (Test-Path -LiteralPath $key)) {
    $registered = Get-ItemProperty -LiteralPath $key
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($registered.InstallLocation, $installDir)) {
        throw 'Registered installation location changed; uninstall cancelled.'
    }
}
$ownedPaths = @((Join-Path $installDir 'LoDeskStream.exe'), (Join-Path $installDir 'manager\LoDeskStreamManager.exe'))
foreach ($process in [Diagnostics.Process]::GetProcesses()) {
    try {
        if ($process.ProcessName -notin @('LoDeskStream','LoDeskStreamManager')) { continue }
        $path = $process.MainModule.FileName
        if ($ownedPaths -contains $path) {
            $process.Kill()
            if (-not $process.WaitForExit(10000)) { throw 'Application did not stop.' }
        }
    } finally { $process.Dispose() }
}
Set-Location $env:TEMP
# Retain the marker and this helper until payload removal succeeds, so failures can be retried.
Get-ChildItem -LiteralPath $installDir -Force |
    Where-Object { $_.Name -notin @('.lodeskstream-install.json','uninstall-lodeskstream.ps1') } |
    Remove-Item -Recurse -Force
if ($marker.Shortcuts) {
    $paths = @(
        (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'LoDeskStream.lnk'),
        (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\LoDeskStream\LoDeskStream.lnk')
    )
    $shell = New-Object -ComObject WScript.Shell
    foreach ($path in $paths) {
        if ((Test-Path -LiteralPath $path) -and
            [StringComparer]::OrdinalIgnoreCase.Equals($shell.CreateShortcut($path).TargetPath, $ownedPaths[1])) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}
if ($marker.Registration) {
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
    }
}
Remove-Item -LiteralPath $installDir -Recurse -Force
if (-not $Quiet) { Write-Host 'LoDeskStream uninstalled.' }
} finally { $setupLock.Dispose() }
""";

    private sealed class MetadataSnapshot
    {
        private readonly Dictionary<string, byte[]?> shortcuts = [];
        private readonly HashSet<string> shortcutDirectories = new(StringComparer.OrdinalIgnoreCase);
        private readonly Dictionary<string, (object Value, RegistryValueKind Kind)> values = [];
        private readonly bool captureRegistry;
        private readonly bool registryExisted;

        public MetadataSnapshot(bool captureShortcuts, bool captureRegistry)
        {
            if (captureShortcuts)
                foreach (var path in ShortcutPaths())
                {
                    shortcuts[path] = File.Exists(path) ? File.ReadAllBytes(path) : null;
                    var directory = Path.GetDirectoryName(path)!;
                    if (Directory.Exists(directory)) shortcutDirectories.Add(directory);
                }
            this.captureRegistry = captureRegistry;
            if (!captureRegistry) return;
            using var key = Registry.CurrentUser.OpenSubKey(RegistryPath);
            registryExisted = key is not null;
            if (key is not null)
                foreach (var name in key.GetValueNames())
                    values[name] = (key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames)!, key.GetValueKind(name));
        }

        public void Restore()
        {
            foreach (var entry in shortcuts)
            {
                if (entry.Value is null) File.Delete(entry.Key);
                else File.WriteAllBytes(entry.Key, entry.Value);
                var directory = Path.GetDirectoryName(entry.Key)!;
                if (!shortcutDirectories.Contains(directory) && Directory.Exists(directory) &&
                    !Directory.EnumerateFileSystemEntries(directory).Any())
                    Directory.Delete(directory);
            }
            if (!captureRegistry) return;
            if (!registryExisted) { Registry.CurrentUser.DeleteSubKeyTree(RegistryPath, throwOnMissingSubKey: false); return; }
            using var key = Registry.CurrentUser.CreateSubKey(RegistryPath, writable: true)
                ?? throw new InvalidOperationException("Could not restore registry metadata.");
            foreach (var name in key.GetValueNames()) key.DeleteValue(name);
            foreach (var entry in values) key.SetValue(entry.Key, entry.Value.Value, entry.Value.Kind);
        }
    }

    private sealed record InstallMarker(string Application, string Version, string InstallDir, bool Shortcuts, bool Registration);
}
