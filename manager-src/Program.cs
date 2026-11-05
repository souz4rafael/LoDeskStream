using System.Diagnostics;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Windows.Forms;
using System.Security.Principal;
using System.Text;
using LoDeskStream;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        ServerLaunch launch;
        try { launch = ServerLaunch.Parse(args, LoDeskStreamTrayContext.GetServerPath()); }
        catch (Exception exc) when (exc is ArgumentException or IOException or UnauthorizedAccessException)
        {
            MessageBox.Show(exc.Message, "LoDeskStream launch failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        var root = Path.GetFullPath(launch.Identity)
            .TrimEnd(Path.DirectorySeparatorChar).ToUpperInvariant();
        var identity = WindowsIdentity.GetCurrent().User?.Value ?? Environment.UserName;
        var key = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes($"{identity}|{root}")));
        using var mutex = new Mutex(true, $@"Local\LoDeskStream.Tray.{key}", out var created);
        if (!created) return;
        Application.Run(new LoDeskStreamTrayContext(launch));
    }
}

internal sealed class LoDeskStreamTrayContext : ApplicationContext
{
    private const int Port = 8765;
    private readonly Icon _trayIcon;
    private readonly NotifyIcon _notifyIcon;
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly ToolStripMenuItem _openLocalItem;
    private readonly ToolStripMenuItem _openLanItem;
    private readonly ToolStripMenuItem _copyTokenItem;
    private readonly ToolStripMenuItem _setTokenItem;
    private readonly ToolStripMenuItem _generateTokenItem;
    private readonly ToolStripMenuItem _virtualStatusItem;
    private readonly ToolStripMenuItem _virtualOnItem;
    private readonly ToolStripMenuItem _virtualOffItem;
    private readonly ToolStripMenuItem _virtualInstallItem;
    private readonly VirtualMonitorController _virtualMonitor = new();
    private VirtualMonitorStatus _virtualStatus = new(VirtualMonitorState.Failed, "Open menu to check driver status");
    private readonly System.Windows.Forms.Timer _timer;
    private readonly ServerController _controller;
    private bool _busy;
    private bool _exiting;
    private bool _exitReady;
    private bool _virtualOperationPending;
    private ServerStatus _status = new(ServerState.Stopped);

    public LoDeskStreamTrayContext(ServerLaunch launch)
    {
        var initialToken = launch.IsDevelopment
            ? Environment.GetEnvironmentVariable("LODESKSTREAM_DEV_TOKEN", EnvironmentVariableTarget.User)
            : null;
        _controller = new ServerController(Port, launch.CreateStartInfo, initialToken: initialToken);
        _statusItem = new ToolStripMenuItem("Status: checking...") { Enabled = false };
        _startItem = new ToolStripMenuItem("Start server", null, async (_, _) => await RunOperationAsync(StartServerAsync));
        _stopItem = new ToolStripMenuItem("Stop owned server", null, async (_, _) => await RunOperationAsync(async () =>
        {
            _status = await _controller.StopAsync();
            ShowInfo("LoDeskStream stopped", "The owned server has stopped.");
        }));
        _openLocalItem = new ToolStripMenuItem("Open local web app", null, async (_, _) => await OpenAppAsync(false));
        _openLanItem = new ToolStripMenuItem("Open LAN web app", null, async (_, _) => await OpenAppAsync(true));
        _copyTokenItem = new ToolStripMenuItem("Copy token", null, async (_, _) => await RunOperationAsync(CopyTokenAsync));
        _setTokenItem = new ToolStripMenuItem("Set password/token...", null, async (_, _) => await RunOperationAsync(SetTokenFromPromptAsync));
        _generateTokenItem = new ToolStripMenuItem("Generate new password", null, async (_, _) => await RunOperationAsync(async () => await SetTokenAsync(ServerController.GenerateToken())));
        _virtualStatusItem = new ToolStripMenuItem("Virtual Monitor: checking…") { Enabled = false };
        _virtualOnItem = new ToolStripMenuItem("Virtual Monitor On", null, async (_, _) => await RunOperationAsync(() => ChangeVirtualMonitorAsync(true)));
        _virtualOffItem = new ToolStripMenuItem("Virtual Monitor Off", null, async (_, _) => await RunOperationAsync(() => ChangeVirtualMonitorAsync(false)));
        _virtualInstallItem = new ToolStripMenuItem("Install Virtual Monitor…", null, async (_, _) => await RunOperationAsync(InstallVirtualMonitorAsync));

        var menu = new ContextMenuStrip();
        menu.Items.AddRange(new ToolStripItem[]
        {
            _statusItem,
            new ToolStripMenuItem(launch.IsDevelopment ? "Mode: development (Python source)" : "Mode: packaged") { Enabled = false },
            new ToolStripSeparator(),
            _startItem,
            _stopItem,
            new ToolStripSeparator(),
            _openLocalItem,
            _openLanItem,
            _copyTokenItem,
            _setTokenItem,
            _generateTokenItem,
            new ToolStripSeparator(),
            _virtualStatusItem,
            _virtualOnItem,
            _virtualOffItem,
            _virtualInstallItem,
            new ToolStripSeparator(),
            new ToolStripMenuItem("Exit tray", null, (_, _) => ExitThread())
        });

        _trayIcon = BrandIcon.Create();
        _notifyIcon = new NotifyIcon
        {
            Icon = _trayIcon,
            Text = "LoDeskStream",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _notifyIcon.DoubleClick += async (_, _) => await OpenAppAsync(false);

        _timer = new System.Windows.Forms.Timer { Interval = 1500 };
        _timer.Tick += async (_, _) => await RunOperationAsync(() => Task.CompletedTask);
        menu.Opening += async (_, _) => await RunOperationAsync(async () =>
        {
            _virtualStatus = new(VirtualMonitorState.Failed, "Checking Windows driver status…");
            ApplyStatus();
            _virtualStatus = await _virtualMonitor.RefreshAsync();
        });
        ApplyStatus();
        _ = RunOperationAsync(StartServerAsync);
    }

    private async Task RunOperationAsync(Func<Task> operation)
    {
        if (_busy || _exiting) return;
        _busy = true;
        _timer.Stop();
        ApplyStatus();
        try
        {
            try { await operation(); }
            catch (Exception exc)
            {
                if (!_exiting) _notifyIcon.ShowBalloonTip(5000, "LoDeskStream operation failed", exc.Message, ToolTipIcon.Warning);
            }
            if (!_exiting) _status = await _controller.RefreshAsync();
        }
        catch (Exception exc)
        {
            _status = new(ServerState.ProbeFailed);
            if (!_exiting) _notifyIcon.ShowBalloonTip(5000, "LoDeskStream status check failed", exc.Message, ToolTipIcon.Warning);
        }
        finally
        {
            _busy = false;
            if (!_exiting)
            {
                ApplyStatus();
                _timer.Start();
            }
        }
    }

    private void ApplyStatus()
    {
        _statusItem.Text = _status.State switch
        {
            ServerState.Owned => $"Status: running - owned (port {Port})",
            ServerState.External => "Status: running - independent server (open only)",
            ServerState.PortOccupied => $"Status: port {Port} occupied by an unknown listener",
            ServerState.OwnedUnhealthy => "Status: server unavailable / operation failed",
            ServerState.ProbeFailed => "Status: probe failed - ownership unknown",
            _ => "Status: stopped",
        };
        _startItem.Enabled = !_busy && _status.State == ServerState.Stopped;
        _stopItem.Enabled = !_busy && _status.CanStopOwned;
        _copyTokenItem.Enabled = _setTokenItem.Enabled = _generateTokenItem.Enabled = !_busy && _status.CanManage;
        _openLocalItem.Enabled = _openLanItem.Enabled = !_busy && _status.CanOpen;
        _virtualStatusItem.Text = _busy ? "Virtual Monitor: operation in progress…" : _virtualStatus.State switch
        {
            VirtualMonitorState.On => "Virtual Monitor: On (driver enabled)",
            VirtualMonitorState.Off => "Virtual Monitor: Off (driver disabled)",
            _ => $"Virtual Monitor: {_virtualStatus.Detail}",
        };
        _virtualStatusItem.ToolTipText = _virtualStatus.Detail;
        _virtualOnItem.Enabled = !_busy && _virtualStatus.State == VirtualMonitorState.Off;
        _virtualOffItem.Enabled = !_busy && _virtualStatus.State == VirtualMonitorState.On;
        _virtualOnItem.ToolTipText = _virtualOffItem.ToolTipText = _virtualStatus.Detail;
        _virtualInstallItem.Enabled = !_busy;
        _notifyIcon.Text = _status.State switch
        {
            ServerState.Owned => "LoDeskStream - owned server",
            ServerState.External => "LoDeskStream - independent server",
            ServerState.PortOccupied => "LoDeskStream - port occupied",
            ServerState.OwnedUnhealthy => "LoDeskStream - unavailable",
            ServerState.ProbeFailed => "LoDeskStream - probe failed",
            _ => "LoDeskStream - stopped",
        };
    }

    private async Task StartServerAsync()
    {
        _status = await _controller.StartAsync();
        if (_status.CanManage)
            ShowInfo("LoDeskStream started", $"Open {GetLanUrl()}. Use Copy token to get the password.");
        else if (_status.State == ServerState.PortOccupied)
            throw new InvalidOperationException($"Port {Port} is occupied by an unknown listener. No server was started.");
    }

    private async Task ChangeVirtualMonitorAsync(bool enabled)
    {
        _virtualOperationPending = true;
        try
        {
            _virtualStatus = await _virtualMonitor.RefreshAsync();
            if (_exiting) return;
            if (!_virtualStatus.CanChange) throw new InvalidOperationException(_virtualStatus.Detail);
            if (MessageBox.Show(
                $"Turn the existing Virtual Display Driver {(enabled ? "on" : "off")}?\n\n" +
                $"Device: {_virtualStatus.InstanceId}\n\n" +
                "This changes all screens created by this driver, including screens used by other apps. " +
                "Windows administrator approval is required; the server and tray remain unelevated.\n\n" +
                "Disconnect browser clients before changing displays. Afterwards reconnect and use the existing Choose monitor picker. " +
                "No automatic physical-monitor fallback is performed.",
                "LoDeskStream virtual monitor", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK) return;
            _virtualStatus = await _virtualMonitor.ChangeAsync(_virtualStatus, enabled);
            ShowInfo("Virtual monitor driver", enabled
                ? "Windows reports the driver enabled. Reconnect in the browser and choose a monitor; display layout is managed in Windows."
                : "Windows reports the driver disabled. Reconnect in the browser and choose a remaining monitor.");
        }
        finally
        {
            try { _virtualStatus = await _virtualMonitor.RefreshAsync(); }
            finally { _virtualOperationPending = false; }
        }
    }

    private Task InstallVirtualMonitorAsync()
    {
        if (MessageBox.Show(VirtualMonitorController.InstallationInstructions,
            "Install Virtual Monitor — manual driver-only setup", MessageBoxButtons.OKCancel,
            MessageBoxIcon.Information) == DialogResult.OK)
            Process.Start(new ProcessStartInfo(VirtualMonitorController.ReleaseUrl) { UseShellExecute = true });
        return Task.CompletedTask;
    }

    private async Task CopyTokenAsync()
    {
        var token = await _controller.GetOwnedTokenAsync();
        if (_exiting) return;
        Clipboard.SetText(token);
        ShowInfo("Token copied", "The owned server's session token is on the clipboard.");
    }

    private async Task SetTokenFromPromptAsync()
    {
        var token = await _controller.GetOwnedTokenAsync();
        if (_exiting) return;
        using var dialog = new TokenDialog(token);
        if (dialog.ShowDialog() != DialogResult.OK)
        {
            return;
        }

        await SetTokenAsync(dialog.Token);
    }

    private async Task SetTokenAsync(string token)
    {
        token = token.Trim();
        if (token.Length < 6)
        {
            MessageBox.Show(
                "Use at least 6 characters.",
                "LoDeskStream password",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return;
        }

        _status = await _controller.ChangeTokenAsync(token);
        ShowInfo("Password set", "The owned server restarted with the new password. Use Copy token to copy it.");
    }

    protected override void ExitThreadCore()
    {
        if (_exitReady) { base.ExitThreadCore(); return; }
        if (_virtualOperationPending)
        {
            ShowInfo("Virtual monitor operation pending", "Finish or cancel administrator approval and wait for the Windows status check before exiting.");
            return;
        }
        if (_exiting) return;
        _exiting = true;
        _timer.Stop();
        _ = FinishExitAsync();
    }

    private async Task FinishExitAsync()
    {
        try
        {
            await _controller.DisposeAsync();
            _timer.Dispose();
            _notifyIcon.Visible = false;
            _notifyIcon.Dispose();
            _trayIcon.Dispose();
            _exitReady = true;
            ExitThread();
        }
        catch (Exception exc)
        {
            _exiting = false;
            _notifyIcon.ShowBalloonTip(5000, "Exit failed", exc.Message, ToolTipIcon.Warning);
            ApplyStatus();
            _timer.Start();
        }
    }

    internal static string GetServerPath()
    {
        var baseDir = AppContext.BaseDirectory;
        var installRoot = Directory.GetParent(baseDir.TrimEnd(Path.DirectorySeparatorChar))?.FullName ?? baseDir;
        var sibling = Path.Combine(installRoot, "LoDeskStream.exe");
        if (File.Exists(sibling))
        {
            return sibling;
        }

        return Path.Combine(baseDir, "LoDeskStream.exe");
    }

    private static string GetLanUrl()
    {
        return $"http://{GetLanAddress()}:{Port}";
    }

    private static string GetLanAddress()
    {
        foreach (var networkInterface in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (networkInterface.OperationalStatus != OperationalStatus.Up ||
                networkInterface.NetworkInterfaceType == NetworkInterfaceType.Loopback)
            {
                continue;
            }

            foreach (var address in networkInterface.GetIPProperties().UnicastAddresses)
            {
                if (address.Address.AddressFamily == AddressFamily.InterNetwork &&
                    !IPAddress.IsLoopback(address.Address))
                {
                    return address.Address.ToString();
                }
            }
        }

        return "localhost";
    }

    private Task OpenAppAsync(bool lan) => RunOperationAsync(async () =>
    {
        _status = await _controller.RefreshAsync();
        if (_exiting) return;
        if (!_status.CanOpen) throw new InvalidOperationException("No recognized healthy LoDeskStream server is available.");
        Process.Start(new ProcessStartInfo(lan ? GetLanUrl() : $"http://localhost:{Port}") { UseShellExecute = true });
    });

    private void ShowInfo(string title, string message)
    {
        if (!_exiting) _notifyIcon.ShowBalloonTip(3500, title, message, ToolTipIcon.Info);
    }
}

internal sealed class TokenDialog : Form
{
    private readonly TextBox _tokenBox;

    public string Token => _tokenBox.Text;

    public TokenDialog(string currentToken)
    {
        Text = "LoDeskStream password/token";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
        ClientSize = new Size(420, 136);

        var label = new Label
        {
            Text = "Password/token remote browsers must enter:",
            AutoSize = true,
            Left = 12,
            Top = 14,
        };
        _tokenBox = new TextBox
        {
            Left = 12,
            Top = 40,
            Width = 396,
            Text = currentToken,
            UseSystemPasswordChar = false,
        };
        var generateButton = new Button
        {
            Text = "Generate",
            Left = 12,
            Top = 82,
            Width = 90,
        };
        var okButton = new Button
        {
            Text = "OK",
            DialogResult = DialogResult.OK,
            Left = 222,
            Top = 82,
            Width = 90,
        };
        var cancelButton = new Button
        {
            Text = "Cancel",
            DialogResult = DialogResult.Cancel,
            Left = 318,
            Top = 82,
            Width = 90,
        };

        generateButton.Click += (_, _) => _tokenBox.Text = GenerateToken();
        Controls.AddRange(new Control[] { label, _tokenBox, generateButton, okButton, cancelButton });
        AcceptButton = okButton;
        CancelButton = cancelButton;
    }

    private static string GenerateToken()
    {
        return Convert.ToBase64String(RandomNumberGenerator.GetBytes(18))
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
    }
}
