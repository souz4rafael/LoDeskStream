using System.Diagnostics;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new LoDeskStreamTrayContext());
    }
}

internal sealed class LoDeskStreamTrayContext : ApplicationContext
{
    private const int Port = 8765;
    private readonly NotifyIcon _notifyIcon;
    private readonly ToolStripMenuItem _statusItem;
    private readonly ToolStripMenuItem _startItem;
    private readonly ToolStripMenuItem _stopItem;
    private readonly ToolStripMenuItem _openLocalItem;
    private readonly ToolStripMenuItem _openLanItem;
    private readonly ToolStripMenuItem _copyTokenItem;
    private readonly ToolStripMenuItem _setTokenItem;
    private readonly ToolStripMenuItem _generateTokenItem;
    private readonly System.Windows.Forms.Timer _timer;
    private Process? _serverProcess;
    private string _token = GenerateToken();

    public LoDeskStreamTrayContext()
    {
        _statusItem = new ToolStripMenuItem("Status: stopped") { Enabled = false };
        _startItem = new ToolStripMenuItem("Start server", null, (_, _) => StartServer());
        _stopItem = new ToolStripMenuItem("Stop server", null, (_, _) => StopServer());
        _openLocalItem = new ToolStripMenuItem("Open local web app", null, (_, _) => OpenUrl($"http://localhost:{Port}"));
        _openLanItem = new ToolStripMenuItem("Open LAN web app", null, (_, _) => OpenUrl(GetLanUrl()));
        _copyTokenItem = new ToolStripMenuItem("Copy token", null, (_, _) => CopyToken());
        _setTokenItem = new ToolStripMenuItem("Set password/token...", null, (_, _) => SetTokenFromPrompt());
        _generateTokenItem = new ToolStripMenuItem("Generate new password", null, (_, _) => GenerateNewToken());

        var menu = new ContextMenuStrip();
        menu.Items.AddRange(new ToolStripItem[]
        {
            _statusItem,
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
            new ToolStripMenuItem("Exit tray", null, (_, _) => ExitThread())
        });

        _notifyIcon = new NotifyIcon
        {
            Icon = Icon.ExtractAssociatedIcon(GetServerPath()) ?? SystemIcons.Application,
            Text = "LoDeskStream",
            ContextMenuStrip = menu,
            Visible = true,
        };
        _notifyIcon.DoubleClick += (_, _) => OpenUrl($"http://localhost:{Port}");

        _timer = new System.Windows.Forms.Timer { Interval = 1500 };
        _timer.Tick += async (_, _) => await RefreshStatusAsync();
        _timer.Start();

        StartServer();
    }

    private async Task RefreshStatusAsync()
    {
        var running = await IsServerHealthyAsync();
        _statusItem.Text = running
            ? $"Status: running ({GetLanUrl()})"
            : "Status: stopped";
        _startItem.Enabled = !running;
        _stopItem.Enabled = running;
        _openLocalItem.Enabled = running;
        _openLanItem.Enabled = running;
        _copyTokenItem.Enabled = running;
        _notifyIcon.Text = running ? "LoDeskStream - running" : "LoDeskStream - stopped";
    }

    private void StartServer()
    {
        if (_serverProcess is { HasExited: false } || IsPortListening())
        {
            _ = RefreshStatusAsync();
            return;
        }

        var serverPath = GetServerPath();
        var startInfo = new ProcessStartInfo
        {
            FileName = serverPath,
            WorkingDirectory = Path.GetDirectoryName(serverPath)!,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        startInfo.Environment["STREAM_TOKEN"] = _token;
        startInfo.Environment["STREAM_PORT"] = Port.ToString();

        _serverProcess = Process.Start(startInfo);
        _notifyIcon.ShowBalloonTip(
            5000,
            "LoDeskStream started",
            $"Open {GetLanUrl()}\nToken: {_token}",
            ToolTipIcon.Info);
        _ = RefreshStatusAsync();
    }

    private void StopServer()
    {
        var stopped = false;
        if (_serverProcess is { HasExited: false })
        {
            _serverProcess.Kill(entireProcessTree: true);
            _serverProcess.Dispose();
            _serverProcess = null;
            stopped = true;
        }

        foreach (var process in Process.GetProcessesByName("LoDeskStream"))
        {
            try
            {
                var serverPath = GetServerPath();
                if (string.Equals(process.MainModule?.FileName, serverPath, StringComparison.OrdinalIgnoreCase))
                {
                    process.Kill(entireProcessTree: true);
                    stopped = true;
                }
            }
            catch
            {
                // Ignore inaccessible processes; the health check will keep status accurate.
            }
        }

        if (stopped)
        {
            _notifyIcon.ShowBalloonTip(2500, "LoDeskStream stopped", "The server is no longer running.", ToolTipIcon.Info);
        }
        _ = RefreshStatusAsync();
    }

    private void CopyToken()
    {
        try
        {
            Clipboard.SetText(_token);
            _notifyIcon.ShowBalloonTip(2500, "Token copied", "The current session token is on the clipboard.", ToolTipIcon.Info);
        }
        catch (Exception exc)
        {
            _notifyIcon.ShowBalloonTip(5000, "Copy failed", $"Token: {_token}\n{exc.Message}", ToolTipIcon.Warning);
        }
    }

    private void SetTokenFromPrompt()
    {
        using var dialog = new TokenDialog(_token);
        if (dialog.ShowDialog() != DialogResult.OK)
        {
            return;
        }

        SetToken(dialog.Token);
    }

    private void GenerateNewToken()
    {
        SetToken(GenerateToken());
    }

    private void SetToken(string token)
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

        var wasRunning = _serverProcess is { HasExited: false } || IsPortListening();
        if (wasRunning)
        {
            StopServer();
        }

        _token = token;
        CopyToken();

        if (wasRunning)
        {
            StartServer();
        }
        else
        {
            _notifyIcon.ShowBalloonTip(3500, "Password set", "Start the server to use the new password.", ToolTipIcon.Info);
        }
    }

    protected override void ExitThreadCore()
    {
        StopServer();
        _timer.Stop();
        _notifyIcon.Visible = false;
        _notifyIcon.Dispose();
        base.ExitThreadCore();
    }

    private static string GetServerPath()
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

    private static async Task<bool> IsServerHealthyAsync()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(750) };
            var response = await client.GetAsync($"http://127.0.0.1:{Port}/health");
            return response.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    private static bool IsPortListening()
    {
        return IPGlobalProperties.GetIPGlobalProperties()
            .GetActiveTcpListeners()
            .Any(endpoint => endpoint.Port == Port);
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

    private static void OpenUrl(string url)
    {
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    private static string GenerateToken()
    {
        return Convert.ToBase64String(RandomNumberGenerator.GetBytes(18))
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
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
