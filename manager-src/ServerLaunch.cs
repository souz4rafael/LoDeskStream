using System.Diagnostics;

namespace LoDeskStream;

internal sealed record ServerLaunch(string Executable, string WorkingDirectory, string? Script = null)
{
    public bool IsDevelopment => Script is not null;
    public string Identity => Path.Combine(WorkingDirectory, Script ?? "LoDeskStream.exe");

    public ProcessStartInfo CreateStartInfo()
    {
        var info = new ProcessStartInfo(Executable)
        {
            WorkingDirectory = WorkingDirectory,
            CreateNoWindow = true,
        };
        if (Script is not null) info.ArgumentList.Add(Script);
        return info;
    }

    public static ServerLaunch Parse(string[] args, string packagedServer)
    {
        if (args.Length == 0)
            return new(packagedServer, Path.GetDirectoryName(packagedServer)!);
        if (args.Length != 4 || args[0] != "--dev-root" || args[2] != "--python")
            throw new ArgumentException("Usage: LoDeskStreamManager [--dev-root <absolute source folder> --python <absolute python.exe>]");
        if (!Path.IsPathFullyQualified(args[1]) || !Path.IsPathFullyQualified(args[3]))
            throw new ArgumentException("Development source and Python paths must be absolute.");
        var root = Path.GetFullPath(args[1]);
        var python = Path.GetFullPath(args[3]);
        var script = Path.Combine(root, "server.py");
        if (!File.Exists(script)) throw new FileNotFoundException("Development server.py not found.", script);
        if (!File.Exists(python)) throw new FileNotFoundException("Development Python executable not found.", python);
        return new(python, root, script);
    }
}
