using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;

const string appName = "LoDeskStream";
const string resourceName = "LoDeskStream-Windows-portable.zip";

var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
var installDir = Path.Combine(localAppData, "Programs", appName);
var startMenuDir = Path.Combine(
    Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),
    "Programs",
    appName);
var desktopDir = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);

Console.WriteLine("LoDeskStream Setup");
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
    var scriptPath = Path.Combine(installDir, "run-lodeskstream.ps1");
    var iconPath = Path.Combine(installDir, "LoDeskStream.exe");
    var escapedShortcut = shortcutPath.Replace("'", "''");
    var escapedScript = scriptPath.Replace("'", "''");
    var escapedWork = installDir.Replace("'", "''");
    var escapedIcon = iconPath.Replace("'", "''");
    var script =
        "$shell = New-Object -ComObject WScript.Shell; " +
        $"$shortcut = $shell.CreateShortcut('{escapedShortcut}'); " +
        "$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'; " +
        $"$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File \"{escapedScript}\"'; " +
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
