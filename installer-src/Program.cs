using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using Microsoft.Win32;

const string appName = "LoDeskStream";
const string resourceName = "LoDeskStream-Windows-portable.zip";
const string publisher = "souz4rafael";
const string version = "0.1.3";

var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
var installDir = Path.Combine(localAppData, "Programs", appName);
var startMenuDir = Path.Combine(
    Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
    "Programs",
    appName);
var desktopDir = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);

Console.WriteLine("LoDeskStream Setup");
if (args.Contains("--uninstall", StringComparer.OrdinalIgnoreCase))
{
    Uninstall();
    if (!args.Contains("--quiet", StringComparer.OrdinalIgnoreCase))
    {
        Console.WriteLine("LoDeskStream uninstalled. Press any key to close.");
        Console.ReadKey(intercept: true);
    }
    return;
}

Console.WriteLine($"Installing to: {installDir}");

if (Directory.Exists(installDir))
{
    Directory.Delete(installDir, recursive: true);
}
Directory.CreateDirectory(installDir);

await using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream(resourceName)
    ?? throw new InvalidOperationException($"Missing embedded resource: {resourceName}"))
{
    using var archive = new ZipArchive(payload, ZipArchiveMode.Read);
    archive.ExtractToDirectory(installDir, overwriteFiles: true);
}

Directory.CreateDirectory(startMenuDir);
CreateShortcut(Path.Combine(startMenuDir, "LoDeskStream.lnk"), installDir);
CreateShortcut(Path.Combine(desktopDir, "LoDeskStream.lnk"), installDir);
CreateUninstallScript(installDir);
RegisterUninstaller(installDir);

Console.WriteLine();
Console.WriteLine("LoDeskStream installed successfully.");
Console.WriteLine("Start it from the Desktop or Start Menu shortcut.");
if (!args.Contains("--quiet", StringComparer.OrdinalIgnoreCase))
{
    Console.WriteLine();
    Console.WriteLine("Press any key to close.");
    Console.ReadKey(intercept: true);
}

static void CreateShortcut(string shortcutPath, string installDir)
{
    var managerPath = Path.Combine(installDir, "manager", "LoDeskStreamManager.exe");
    var iconPath = Path.Combine(installDir, "LoDeskStream.exe");
    var escapedShortcut = shortcutPath.Replace("'", "''");
    var escapedManager = managerPath.Replace("'", "''");
    var escapedWork = installDir.Replace("'", "''");
    var escapedIcon = iconPath.Replace("'", "''");
    var script =
        "$shell = New-Object -ComObject WScript.Shell; " +
        $"$shortcut = $shell.CreateShortcut('{escapedShortcut}'); " +
        $"$shortcut.TargetPath = '{escapedManager}'; " +
        "$shortcut.Arguments = ''; " +
        $"$shortcut.WorkingDirectory = '{escapedWork}'; " +
        $"$shortcut.IconLocation = '{escapedIcon}'; " +
        "$shortcut.Save();";

    var startInfo = new ProcessStartInfo
    {
        FileName = "powershell.exe",
        Arguments = "-NoProfile -ExecutionPolicy Bypass -Command " + Quote(script),
        UseShellExecute = false,
        CreateNoWindow = true,
    };
    using var process = Process.Start(startInfo) ?? throw new InvalidOperationException("Unable to start PowerShell.");
    process.WaitForExit();
    if (process.ExitCode != 0)
    {
        throw new InvalidOperationException($"Failed to create shortcut: {shortcutPath}");
    }
}

static string Quote(string value)
{
    return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
}

static void RegisterUninstaller(string installDir)
{
    var uninstallScript = Path.Combine(installDir, "uninstall-lodeskstream.ps1");
    var keyPath = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream";
    using var key = Registry.CurrentUser.CreateSubKey(keyPath, writable: true)
        ?? throw new InvalidOperationException("Unable to create uninstall registry key.");
    key.SetValue("DisplayName", appName);
    key.SetValue("DisplayVersion", version);
    key.SetValue("Publisher", publisher);
    key.SetValue("InstallLocation", installDir);
    key.SetValue("DisplayIcon", Path.Combine(installDir, "LoDeskStream.exe"));
    key.SetValue("UninstallString", $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{uninstallScript}\"");
    key.SetValue("QuietUninstallString", $"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"{uninstallScript}\" -Quiet");
    key.SetValue("NoModify", 1, RegistryValueKind.DWord);
    key.SetValue("NoRepair", 1, RegistryValueKind.DWord);
}

static void CreateUninstallScript(string installDir)
{
    var startMenuPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
        "Programs",
        appName);
    var desktopShortcut = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        "LoDeskStream.lnk");
    var escapedInstall = installDir.Replace("'", "''");
    var escapedStart = startMenuPath.Replace("'", "''");
    var escapedDesktop = desktopShortcut.Replace("'", "''");
    var script = $$"""
param([switch]$Quiet)
$ErrorActionPreference = 'SilentlyContinue'
Get-Process LoDeskStreamManager | ForEach-Object { Stop-Process -Id $_.Id -Force }
Get-Process LoDeskStream | ForEach-Object { Stop-Process -Id $_.Id -Force }
Remove-Item '{{escapedDesktop}}' -Force
Remove-Item '{{escapedStart}}' -Recurse -Force
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream' -Recurse -Force
$installDir = '{{escapedInstall}}'
$cmd = "Start-Sleep -Seconds 1; Remove-Item '$installDir' -Recurse -Force"
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',$cmd
if (-not $Quiet) {
    Write-Host 'LoDeskStream uninstall started. The install folder will be removed shortly.'
    Start-Sleep -Seconds 2
}
""";
    File.WriteAllText(Path.Combine(installDir, "uninstall-lodeskstream.ps1"), script);
}

static void Uninstall()
{
    var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
    var installDir = Path.Combine(localAppData, "Programs", appName);
    var startMenuDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
        "Programs",
        appName);
    var desktopShortcut = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        "LoDeskStream.lnk");

    foreach (var processName in new[] { "LoDeskStreamManager", "LoDeskStream" })
    {
        foreach (var process in Process.GetProcessesByName(processName))
        {
            try { process.Kill(entireProcessTree: true); } catch { }
        }
    }

    if (File.Exists(desktopShortcut)) File.Delete(desktopShortcut);
    if (Directory.Exists(startMenuDir)) Directory.Delete(startMenuDir, recursive: true);
    Registry.CurrentUser.DeleteSubKeyTree(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\LoDeskStream", throwOnMissingSubKey: false);
    if (Directory.Exists(installDir)) Directory.Delete(installDir, recursive: true);
    Console.WriteLine("LoDeskStream uninstalled.");
}
