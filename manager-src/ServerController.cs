using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Security.Cryptography;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.ComponentModel;
using Microsoft.Win32.SafeHandles;

namespace LoDeskStream;

public enum ServerState { Stopped, Owned, External, PortOccupied, OwnedUnhealthy, ProbeFailed }

public sealed record ServerStatus(ServerState State, bool CanStopOwned = false)
{
    public bool CanOpen => State is ServerState.Owned or ServerState.External;
    public bool CanManage => State == ServerState.Owned;
}

public sealed class ServerController : IAsyncDisposable
{
    private readonly Func<ProcessStartInfo> _createStartInfo;
    private readonly int _port;
    private readonly TimeSpan _startupTimeout;
    private readonly HttpClient _client = new(new HttpClientHandler
    {
        UseProxy = false,
        AllowAutoRedirect = false,
    })
    {
        Timeout = TimeSpan.FromMilliseconds(750),
        MaxResponseContentBufferSize = 16384,
    };
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Process? _process;
    private string? _instanceId;
    private string? _token;
    private string? _configuredToken;
    private bool _disposed;

    public ServerController(int port, Func<ProcessStartInfo> createStartInfo, TimeSpan? startupTimeout = null, string? initialToken = null)
    {
        if (port is < 1 or > 65535) throw new ArgumentOutOfRangeException(nameof(port));
        _port = port;
        _createStartInfo = createStartInfo;
        _startupTimeout = startupTimeout ?? TimeSpan.FromSeconds(45);
        if (initialToken is not null && initialToken.Length < 6)
            throw new ArgumentException("Use at least 6 characters for the initial token.", nameof(initialToken));
        _configuredToken = initialToken;
    }

    public static string GenerateToken() => Convert.ToHexString(RandomNumberGenerator.GetBytes(18)).ToLowerInvariant();

    public async Task<ServerStatus> RefreshAsync()
    {
        await _gate.WaitAsync();
        try { ThrowIfDisposed(); return await ProbeAsync(); }
        finally { _gate.Release(); }
    }

    public async Task<ServerStatus> StartAsync()
    {
        await _gate.WaitAsync();
        try
        {
            ThrowIfDisposed();
            var status = await ProbeAsync();
            if (status.State != ServerState.Stopped) return status;
            return await LaunchAsync(_configuredToken ?? GenerateToken());
        }
        finally { _gate.Release(); }
    }

    public async Task<string> GetOwnedTokenAsync()
    {
        await _gate.WaitAsync();
        try
        {
            ThrowIfDisposed();
            await RequireOwnedAsync();
            return _token!;
        }
        finally { _gate.Release(); }
    }

    public async Task<ServerStatus> ChangeTokenAsync(string token)
    {
        token = token.Trim();
        if (token.Length < 6) throw new ArgumentException("Use at least 6 characters.", nameof(token));
        await _gate.WaitAsync();
        try
        {
            ThrowIfDisposed();
            await RequireOwnedAsync();
            await CleanupChildAsync();
            var status = await ProbeAsync();
            if (status.State != ServerState.Stopped)
                throw new InvalidOperationException("The port is now occupied. The password was not changed.");
            return await LaunchAsync(token);
        }
        finally { _gate.Release(); }
    }

    public async Task<ServerStatus> StopAsync()
    {
        await _gate.WaitAsync();
        try
        {
            ThrowIfDisposed();
            if (_process is not { HasExited: false })
                throw new InvalidOperationException("This tray has no live owned process to stop.");
            await CleanupChildAsync();
            return await ProbeAsync();
        }
        finally { _gate.Release(); }
    }

    private async Task RequireOwnedAsync()
    {
        if (!(await ProbeAsync()).CanManage)
            throw new InvalidOperationException("This tray does not own a matching healthy server. Its password cannot be copied or changed here.");
    }

    private async Task<ServerStatus> LaunchAsync(string token)
    {
        await CleanupChildAsync();
        var startInfo = _createStartInfo();
        startInfo.UseShellExecute = false;
        _instanceId = Guid.NewGuid().ToString("N");
        startInfo.Environment["STREAM_INSTANCE_ID"] = _instanceId;
        startInfo.Environment["STREAM_TOKEN"] = token;
        startInfo.Environment["STREAM_PORT"] = _port.ToString(System.Globalization.CultureInfo.InvariantCulture);
        try
        {
            _process = Process.Start(startInfo) ?? throw new InvalidOperationException("The server process could not be started.");
            _token = token;
            var watch = Stopwatch.StartNew();
            while (watch.Elapsed < _startupTimeout)
            {
                if (_process.HasExited)
                    throw new InvalidOperationException($"The server exited during startup (exit code {_process.ExitCode}).");
                var status = await ProbeAsync();
                if (status.CanManage)
                {
                    _configuredToken = token;
                    return status;
                }
                await Task.Delay(200);
            }
            throw new TimeoutException("The server did not report matching healthy status before the startup timeout.");
        }
        catch (Exception startupError)
        {
            try { await CleanupChildAsync(); }
            catch (Exception cleanupError)
            {
                throw new AggregateException("Server startup failed and its child could not be cleaned up.", startupError, cleanupError);
            }
            throw;
        }
    }

    private async Task<ServerStatus> ProbeAsync()
    {
        try
        {
            using var response = await _client.GetAsync($"http://127.0.0.1:{_port}/health");
            if (response.IsSuccessStatusCode)
            {
                using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                var root = json.RootElement;
                if (root.ValueKind == JsonValueKind.Object &&
                    root.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True &&
                    root.TryGetProperty("application", out var app) && app.ValueKind == JsonValueKind.String && app.GetString() == "LoDeskStream" &&
                    root.TryGetProperty("instanceId", out var instance) && instance.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(instance.GetString()) &&
                    root.TryGetProperty("pid", out var pid) && pid.ValueKind == JsonValueKind.Number && pid.TryGetInt32(out var processId) && processId > 0)
                {
                    if (_process is { HasExited: false } && instance.GetString() == _instanceId &&
                        IsOwnedProcessOrDescendant(processId))
                        return new(ServerState.Owned, true);
                    return new(ServerState.External, HasLiveOwnedProcess);
                }
            }
        }
        catch (Exception exc) when (exc is HttpRequestException or TaskCanceledException or JsonException)
        {
            // An unresponsive or non-JSON listener is occupied, never an owned healthy server.
        }

        if (HasLiveOwnedProcess) return new(ServerState.OwnedUnhealthy, true);
        return new(IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners().Any(e => e.Port == _port)
            ? ServerState.PortOccupied : ServerState.Stopped);
    }

    private bool HasLiveOwnedProcess => _process is { HasExited: false };

    private bool IsOwnedProcessOrDescendant(int processId)
    {
        if (_process is not { HasExited: false }) return false;
        if (processId == _process.Id) return true;

        try
        {
            // Hold the reported process handle before taking the snapshot so its PID
            // cannot be recycled between the ancestry snapshot and identity checks.
            using var reportedProcess = Process.GetProcessById(processId);
            _ = reportedProcess.SafeHandle;
            if (reportedProcess.HasExited) return false;

            // PyInstaller can keep a supervisor alive while Python runs in a child.
            // Prove ancestry from OS metadata, not from the PID claimed by HTTP.
            using var snapshot = CreateToolhelp32Snapshot(2, 0);
            if (snapshot.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not verify the server process ancestry.");
            var entry = new ProcessEntry { Size = (uint)Marshal.SizeOf<ProcessEntry>() };
            var parents = new Dictionary<int, int>();
            if (!Process32First(snapshot, ref entry))
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the server process ancestry.");
            do { parents[(int)entry.ProcessId] = (int)entry.ParentProcessId; }
            while (Process32Next(snapshot, ref entry));
            if (Marshal.GetLastWin32Error() != 18)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not finish reading the server process ancestry.");

            var seen = new HashSet<int>();
            var latestStart = DateTime.MaxValue;
            var rootStart = _process.StartTime;
            while (processId != _process.Id)
            {
                if (!seen.Add(processId) || !parents.TryGetValue(processId, out var parentId)) return false;
                using var descendant = Process.GetProcessById(processId);
                var start = descendant.StartTime;
                if (descendant.HasExited || start < rootStart || start > latestStart) return false;
                latestStart = start;
                processId = parentId;
            }
            return !_process.HasExited && !reportedProcess.HasExited;
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry
    {
        public uint Size;
        public uint Usage;
        public uint ProcessId;
        public UIntPtr DefaultHeapId;
        public uint ModuleId;
        public uint Threads;
        public uint ParentProcessId;
        public int Priority;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string ExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", EntryPoint = "Process32FirstW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32First(SafeFileHandle snapshot, ref ProcessEntry entry);

    [DllImport("kernel32.dll", EntryPoint = "Process32NextW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32Next(SafeFileHandle snapshot, ref ProcessEntry entry);

    private async Task CleanupChildAsync()
    {
        if (_process is not null)
        {
            if (!_process.HasExited)
            {
                try { _process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) when (_process.HasExited) { }
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await _process.WaitForExitAsync(timeout.Token);
            }
            _process.Dispose();
            _process = null;
        }
        _instanceId = null;
        _token = null;
    }

    private void ThrowIfDisposed() => ObjectDisposedException.ThrowIf(_disposed, this);

    public async ValueTask DisposeAsync()
    {
        await _gate.WaitAsync();
        try
        {
            if (_disposed) return;
            await CleanupChildAsync();
            _configuredToken = null;
            _disposed = true;
            _client.Dispose();
        }
        finally { _gate.Release(); }
    }
}
