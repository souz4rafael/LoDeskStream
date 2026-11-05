using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using LoDeskStream;

internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        if (args.FirstOrDefault() == "--child") return await ChildAsync(args[1]);
        if (args.FirstOrDefault() == "--real-server") return await RealServerAsync(args);
        if (args.FirstOrDefault() == "--brand-icon")
        {
            await BrandIconTests.RunAsync(args.Length > 1 ? Path.GetFullPath(args[1]) : null);
            Console.WriteLine("PASS embedded brand icon, independent lifetime, arbitrary working directory and executable branding");
            return 0;
        }
        if (args.Length == 1 && args[0] == "--virtual-monitor-probe")
        {
            var status = await new VirtualMonitorController().RefreshAsync();
            Console.WriteLine($"{status.State}: {status.Detail}");
            return status.State == VirtualMonitorState.Failed ? 1 : 0;
        }
        if (args.Length != 0)
        {
            Console.Error.WriteLine("Usage: LoDeskStream.TrayTests [--real-server <absolute LoDeskStream.exe> | --virtual-monitor-probe | --brand-icon [absolute manager.exe]]");
            return 2;
        }
        var tests = new (string Name, Func<Task> Run)[]
        {
            ("development launch configuration and initial password", DevelopmentLaunchAsync),
            ("embedded brand icon and independent lifetime", () => BrandIconTests.RunAsync()),
            ("virtual monitor identity, elevation, errors and script syntax (no display changes)", VirtualMonitorTests.RunAsync),
            ("external server is open-only and survives tray exit", ExternalAsync),
            ("unknown listener cannot be managed", UnknownAsync),
            ("owned token, restart, serialization and stop", OwnedAsync),
            ("startup exit is reported and cleaned up", StartupExitAsync),
            ("startup timeout cleans up only the owned child", StartupTimeoutAsync),
            ("mismatched health identity is not owned", MismatchAsync),
            ("unhealthy owned server cannot expose or change credentials", UnhealthyAsync),
            ("replacement external listener never inherits ownership", ReplacementAsync),
            ("bootloader supervisor child is proven by OS ancestry", SupervisorAsync),
            ("matching nonce cannot make an unrelated PID owned", UnrelatedPidAsync),
            ("successful custom password survives stop/start only in memory", ConfiguredTokenAsync),
        };
        try
        {
            foreach (var test in tests)
            {
                await test.Run();
                Console.WriteLine($"PASS {test.Name}");
            }
            Console.WriteLine($"All {tests.Length} tray tests passed.");
            return 0;
        }
        catch (Exception exc)
        {
            Console.Error.WriteLine(exc);
            return 1;
        }
    }

    private static async Task DevelopmentLaunchAsync()
    {
        var root = Path.Combine(Path.GetTempPath(), $"LoDeskStream-dev-launch-{Guid.NewGuid():N}", "source with spaces");
        Directory.CreateDirectory(root);
        try
        {
            var script = Path.Combine(root, "server.py");
            var python = Path.Combine(root, "python.exe");
            File.WriteAllText(script, "");
            File.WriteAllText(python, "");
            var launch = ServerLaunch.Parse(["--dev-root", root, "--python", python], python);
            var info = launch.CreateStartInfo();
            Check(launch.IsDevelopment && launch.Identity == script, "Wrong development identity.");
            Check(info.FileName == python && info.WorkingDirectory == root &&
                info.ArgumentList.Count == 1 && info.ArgumentList[0] == script,
                "Development arguments do not preserve paths.");
            Check(!ServerLaunch.Parse([], python).IsDevelopment, "Packaged launch changed.");
            await RejectAsync(() => Task.FromResult(ServerLaunch.Parse(["--dev-root", root], python)));
            await RejectAsync(() => Task.FromResult(ServerLaunch.Parse(["--dev-root", ".", "--python", python], python)));
            var port = FreePort();
            await using var controller = new ServerController(port, () => StartInfo(port, "healthy"),
                initialToken: "test-initial-password");
            Check((await controller.StartAsync()).CanManage, "Configured launch failed.");
            Check(await controller.GetOwnedTokenAsync() == "test-initial-password", "Initial password was not retained.");
            await controller.StopAsync();
            Check((await controller.StartAsync()).CanManage, "Configured restart failed.");
            Check(await controller.GetOwnedTokenAsync() == "test-initial-password", "Restart lost configured password.");
        }
        finally { Directory.Delete(Path.GetDirectoryName(root)!, true); }
    }

    private static async Task<int> RealServerAsync(string[] args)
    {
        if (args.Length != 2 || !Path.IsPathFullyQualified(args[1]) || !File.Exists(args[1]))
        {
            Console.Error.WriteLine("--real-server requires the absolute path of an existing LoDeskStream.exe.");
            return 2;
        }
        try
        {
            var path = Path.GetFullPath(args[1]);
            var port = FreePort();
            await using var controller = new ServerController(port, () =>
            {
                var info = new ProcessStartInfo(path)
                {
                    WorkingDirectory = Path.GetDirectoryName(path)!,
                    CreateNoWindow = true,
                };
                info.Environment["STREAM_HOST"] = "127.0.0.1";
                info.Environment["STREAM_AUDIO"] = "0";
                return info;
            });
            var status = await controller.StartAsync();
            Check(status.CanManage && status.CanOpen && status.CanStopOwned, "Packaged server did not prove launch ownership.");
            var token = await controller.GetOwnedTokenAsync();
            Check(token.Length >= 24, "Owned launch did not retain its generated token.");
            using var client = new HttpClient(new HttpClientHandler
            {
                UseProxy = false,
                AllowAutoRedirect = false,
            }) { Timeout = TimeSpan.FromSeconds(10) };
            using var health = JsonDocument.Parse(await client.GetStringAsync($"http://127.0.0.1:{port}/health"));
            using var pythonProcess = Process.GetProcessById(health.RootElement.GetProperty("pid").GetInt32());
            using var unauthenticated = await client.GetAsync($"http://127.0.0.1:{port}/api/monitors");
            Check(unauthenticated.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden, "Monitor endpoint did not reject an unauthenticated request.");
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/api/monitors");
            request.Headers.Add("X-Auth-Token", token);
            using var authenticated = await client.SendAsync(request);
            Check(authenticated.IsSuccessStatusCode, "Owned token did not authenticate to the packaged monitor endpoint.");
            using var monitors = JsonDocument.Parse(await authenticated.Content.ReadAsStringAsync());
            Check(monitors.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True &&
                monitors.RootElement.TryGetProperty("monitors", out var items) && items.ValueKind == JsonValueKind.Array,
                "Authenticated monitor response did not match the server contract.");
            Check((await controller.StopAsync()).State == ServerState.Stopped, "Owned packaged launch did not stop cleanly.");
            await pythonProcess.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
            await controller.DisposeAsync();
            Console.WriteLine($"PASS packaged ownership, token authentication, monitor listing and owned process cleanup (ephemeral port {port}, health PID {pythonProcess.Id}).");
            return 0;
        }
        catch (Exception exc)
        {
            Console.Error.WriteLine($"Packaged server integration failed: {exc.Message}");
            return 1;
        }
    }

    private static ProcessStartInfo StartInfo(int port, string mode)
    {
        var info = new ProcessStartInfo(Environment.ProcessPath!)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        info.ArgumentList.Add("--child");
        info.ArgumentList.Add(mode);
        info.Environment["STREAM_PORT"] = port.ToString();
        return info;
    }

    private static ServerController Controller(int port, string mode = "healthy", TimeSpan? timeout = null)
        => new(port, () => StartInfo(port, mode), timeout ?? TimeSpan.FromSeconds(8));

    private static int FreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static async Task RejectAsync(Func<Task> action, string? text = null)
    {
        try { await action(); }
        catch (Exception exc) when (exc is InvalidOperationException or TimeoutException or ArgumentException)
        {
            if (text is not null) Check(exc.Message.Contains(text, StringComparison.OrdinalIgnoreCase), $"Unexpected failure: {exc}");
            return;
        }
        throw new InvalidOperationException("The operation should have failed.");
    }

    private static async Task CleanupAsync(Process child)
    {
        try
        {
            if (!child.HasExited)
            {
                child.Kill(entireProcessTree: true);
                await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
            }
        }
        finally { child.Dispose(); }
    }

    private static async Task WaitListeningAsync(int port, Process child)
    {
        var watch = Stopwatch.StartNew();
        while (watch.Elapsed < TimeSpan.FromSeconds(8))
        {
            Check(!child.HasExited, "Fake server exited early.");
            using var socket = new TcpClient();
            try { await socket.ConnectAsync(IPAddress.Loopback, port); return; }
            catch (SocketException) { await Task.Delay(50); }
        }
        throw new TimeoutException("Fake server did not listen.");
    }

    private static async Task ExternalAsync()
    {
        var port = FreePort();
        var info = StartInfo(port, "healthy");
        info.Environment["STREAM_INSTANCE_ID"] = "independent-test-instance";
        using var child = Process.Start(info)!;
        try
        {
            await WaitListeningAsync(port, child);
            await using (var controller = Controller(port))
            {
                var state = await controller.StartAsync();
                Check(state.State == ServerState.External && state.CanOpen && !state.CanManage && !state.CanStopOwned, "External server not recognized as open-only.");
                await RejectAsync(async () => await controller.GetOwnedTokenAsync());
                await RejectAsync(async () => await controller.ChangeTokenAsync("new-password"));
                await RejectAsync(async () => await controller.StopAsync());
            }
            Check(!child.HasExited, "Tray exit stopped an external server.");
            using var http = Client();
            Check((await http.GetStringAsync($"http://127.0.0.1:{port}/health")).Contains("independent-test-instance"), "External server changed.");
        }
        finally { await CleanupAsync(child); }
    }

    private static async Task UnknownAsync()
    {
        var port = FreePort();
        using var child = Process.Start(StartInfo(port, "unknown"))!;
        try
        {
            await WaitListeningAsync(port, child);
            await using (var controller = Controller(port))
            {
                var status = await controller.StartAsync();
                Check(status.State == ServerState.PortOccupied && !status.CanOpen && !status.CanManage && !status.CanStopOwned, "Unknown listener reported success.");
                await RejectAsync(async () => await controller.StopAsync());
                await RejectAsync(async () => await controller.GetOwnedTokenAsync());
                await RejectAsync(async () => await controller.ChangeTokenAsync("new-password"));
            }
            Check(!child.HasExited, "Unknown listener was stopped.");
        }
        finally { await CleanupAsync(child); }
    }

    private static HttpClient Client() => new(new HttpClientHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };

    private static async Task<JsonDocument> DetailsAsync(int port)
    {
        using var client = Client();
        return JsonDocument.Parse(await client.GetStringAsync($"http://127.0.0.1:{port}/test-details"));
    }

    private static async Task OwnedAsync()
    {
        var port = FreePort();
        await using var controller = Controller(port);
        var starts = await Task.WhenAll(controller.StartAsync(), controller.StartAsync(), controller.RefreshAsync());
        Check(starts.All(s => s.CanManage && s.CanStopOwned), "Concurrent start/probe lost ownership.");
        var token = await controller.GetOwnedTokenAsync();
        using var first = await DetailsAsync(port);
        var firstPid = first.RootElement.GetProperty("pid").GetInt32();
        var instance = first.RootElement.GetProperty("instanceId").GetString();
        using var firstProcess = Process.GetProcessById(firstPid);
        Check(first.RootElement.GetProperty("token").GetString() == token, "Copied token is not the launched token.");
        Check(token.Length >= 24 && !string.IsNullOrEmpty(instance), "Missing random credentials/identity.");
        await RejectAsync(async () => await controller.ChangeTokenAsync("tiny"));
        Check((await controller.RefreshAsync()).CanManage, "Invalid password interrupted the server.");
        Check((await controller.ChangeTokenAsync("replacement-password")).CanManage, "Password restart did not become healthy.");
        using var second = await DetailsAsync(port);
        Check(firstProcess.HasExited, "Old owned process survived password restart.");
        Check(second.RootElement.GetProperty("instanceId").GetString() != instance, "Restart reused the instance ID.");
        Check(second.RootElement.GetProperty("pid").GetInt32() != firstPid, "Restart reused old process.");
        Check(await controller.GetOwnedTokenAsync() == "replacement-password" && second.RootElement.GetProperty("token").GetString() == "replacement-password", "Password restart mismatch.");
        using var secondProcess = Process.GetProcessById(second.RootElement.GetProperty("pid").GetInt32());
        Check((await controller.StopAsync()).State == ServerState.Stopped, "Owned stop did not clear the port.");
        Check(secondProcess.HasExited, "Owned stop left child alive.");
        await RejectAsync(async () => await controller.GetOwnedTokenAsync());
        Check((await controller.StartAsync()).CanManage, "Owned server could not start after stop.");
        using var third = await DetailsAsync(port);
        using var thirdProcess = Process.GetProcessById(third.RootElement.GetProperty("pid").GetInt32());
        await controller.DisposeAsync();
        Check(thirdProcess.HasExited, "Tray disposal left its own child alive.");
    }

    private static async Task StartupExitAsync()
    {
        var port = FreePort();
        await using var controller = Controller(port, "exit");
        await RejectAsync(async () => await controller.StartAsync(), "exit code 23");
        Check((await controller.RefreshAsync()).State == ServerState.Stopped, "Exited startup child retained running status.");
    }

    private static async Task StartupTimeoutAsync()
    {
        var port = FreePort();
        using var external = Process.Start(StartInfo(FreePort(), "idle"))!;
        try
        {
            var info = StartInfo(port, "idle");
            Process? owned = null;
            await using var controller = new ServerController(port, () => info, TimeSpan.FromSeconds(10));
            // Observe the exact owned PID without writing files or exposing controller internals.
            var pipeName = "LoDeskStreamTrayTests-" + Guid.NewGuid().ToString("N");
            info.Environment["TRAY_TEST_PIPE"] = pipeName;
            using var pipe = new System.IO.Pipes.NamedPipeServerStream(pipeName, System.IO.Pipes.PipeDirection.In, 1, System.IO.Pipes.PipeTransmissionMode.Byte, System.IO.Pipes.PipeOptions.Asynchronous);
            var start = controller.StartAsync();
            try
            {
                await pipe.WaitForConnectionAsync().WaitAsync(TimeSpan.FromSeconds(15));
                using var reader = new StreamReader(pipe);
                var pid = int.Parse((await reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(8)))!);
                owned = Process.GetProcessById(pid);
                await RejectAsync(async () => await start, "timeout");
                Check(owned.HasExited, "Timed-out child was not cleaned up.");
                Check(!external.HasExited, "Timeout cleanup killed an unrelated test process.");
                Check((await controller.RefreshAsync()).State == ServerState.Stopped, "Timeout did not clear ownership.");
            }
            finally
            {
                if (owned is not null) await CleanupAsync(owned);
            }
        }
        finally { await CleanupAsync(external); }
    }

    private static async Task MismatchAsync()
    {
        foreach (var mode in new[] { "wrong-id", "wrong-pid" })
        {
            var port = FreePort();
            await using var controller = Controller(port, mode, TimeSpan.FromMilliseconds(900));
            await RejectAsync(async () => await controller.StartAsync(), "timeout");
            Check((await controller.RefreshAsync()).State == ServerState.Stopped, "Mismatched startup child was not cleaned up.");
        }
    }

    private static async Task UnhealthyAsync()
    {
        var port = FreePort();
        await using var controller = Controller(port);
        await controller.StartAsync();
        using var http = Client();
        await http.GetStringAsync($"http://127.0.0.1:{port}/test-unhealthy");
        var status = await controller.RefreshAsync();
        Check(status.State == ServerState.OwnedUnhealthy && status.CanStopOwned && !status.CanManage, "Lost health still grants password access or prevents recovery stop.");
        await RejectAsync(async () => await controller.GetOwnedTokenAsync());
        await RejectAsync(async () => await controller.ChangeTokenAsync("new-password"));
        Check((await controller.StopAsync()).State == ServerState.Stopped, "Unhealthy owned server could not be stopped.");
    }

    private static async Task ReplacementAsync()
    {
        var port = FreePort();
        await using var controller = Controller(port);
        await controller.StartAsync();
        using var details = await DetailsAsync(port);
        using var owned = Process.GetProcessById(details.RootElement.GetProperty("pid").GetInt32());
        owned.Kill(entireProcessTree: true);
        await owned.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10));
        using var replacement = Process.Start(StartInfo(port, "healthy"))!;
        try
        {
            await WaitListeningAsync(port, replacement);
            var status = await controller.RefreshAsync();
            Check(status.State == ServerState.External && !status.CanStopOwned, "Replacement inherited ownership.");
            await RejectAsync(async () => await controller.GetOwnedTokenAsync());
            await RejectAsync(async () => await controller.ChangeTokenAsync("new-password"));
            await RejectAsync(async () => await controller.StopAsync());
            await controller.DisposeAsync();
            Check(!replacement.HasExited, "Tray exit killed a replacement external server.");
        }
        finally { await CleanupAsync(replacement); }
    }

    private static async Task SupervisorAsync()
    {
        var port = FreePort();
        await using var controller = Controller(port, "supervisor");
        Check((await controller.StartAsync()).CanManage, "Bootloader child was not recognized.");
        using var details = await DetailsAsync(port);
        using var child = Process.GetProcessById(details.RootElement.GetProperty("pid").GetInt32());
        Check(await controller.GetOwnedTokenAsync() == details.RootElement.GetProperty("token").GetString(), "Bootloader token was not retained.");
        Check((await controller.ChangeTokenAsync("supervisor-password")).CanManage, "Bootloader restart failed.");
        Check(child.HasExited, "Bootloader restart left its former child running.");
        using var replacementDetails = await DetailsAsync(port);
        using var replacement = Process.GetProcessById(replacementDetails.RootElement.GetProperty("pid").GetInt32());
        Check((await controller.StopAsync()).State == ServerState.Stopped && replacement.HasExited, "Bootloader tree stop left its Python child alive.");
    }

    private static async Task UnrelatedPidAsync()
    {
        using var unrelated = Process.Start(StartInfo(FreePort(), "idle"))!;
        try
        {
            var port = FreePort();
            await using var controller = new ServerController(port, () =>
            {
                var info = StartInfo(port, "unrelated-pid");
                info.Environment["TRAY_TEST_UNRELATED_PID"] = unrelated.Id.ToString();
                return info;
            }, TimeSpan.FromSeconds(2));
            await RejectAsync(async () => await controller.StartAsync(), "timeout");
            Check(!unrelated.HasExited, "Cleanup killed the PID claimed by health.");
            Check((await controller.RefreshAsync()).State == ServerState.Stopped, "Rejected claimed-PID child was not cleaned up.");
        }
        finally { await CleanupAsync(unrelated); }
    }

    private static async Task ConfiguredTokenAsync()
    {
        var port = FreePort();
        var mode = "healthy";
        await using (var controller = new ServerController(port, () => StartInfo(port, mode), TimeSpan.FromSeconds(8)))
        {
            await controller.StartAsync();
            await controller.ChangeTokenAsync("custom-password-for-session");
            Check((await controller.StopAsync()).State == ServerState.Stopped, "Custom server did not stop.");
            await RejectAsync(async () => await controller.GetOwnedTokenAsync());
            Check((await controller.StartAsync()).CanManage, "Stopped custom server did not restart.");
            Check(await controller.GetOwnedTokenAsync() == "custom-password-for-session", "Stop/start silently replaced the configured password.");
            using var details = await DetailsAsync(port);
            Check(details.RootElement.GetProperty("token").GetString() == "custom-password-for-session", "Restarted child did not receive the configured password.");
            mode = "exit";
            await RejectAsync(async () => await controller.ChangeTokenAsync("unsuccessful-password"), "exit code 23");
            mode = "healthy";
            await controller.StartAsync();
            Check(await controller.GetOwnedTokenAsync() == "custom-password-for-session", "Failed password change replaced the last successful configured password.");
        }
        await using var freshTray = Controller(port);
        await freshTray.StartAsync();
        Check(await freshTray.GetOwnedTokenAsync() != "custom-password-for-session", "Custom password persisted beyond the controller session.");
    }

    private static async Task<int> ChildAsync(string mode)
    {
        if (mode == "exit") return 23;
        if (mode == "supervisor")
        {
            using var child = Process.Start(StartInfo(int.Parse(Environment.GetEnvironmentVariable("STREAM_PORT")!), "healthy"))!;
            try { await child.WaitForExitAsync(); return child.ExitCode; }
            finally { await CleanupAsync(child); }
        }
        var pipeName = Environment.GetEnvironmentVariable("TRAY_TEST_PIPE");
        if (pipeName is not null)
        {
            using var pipe = new System.IO.Pipes.NamedPipeClientStream(".", pipeName, System.IO.Pipes.PipeDirection.Out, System.IO.Pipes.PipeOptions.Asynchronous);
            await pipe.ConnectAsync(15000);
            await using var writer = new StreamWriter(pipe) { AutoFlush = true };
            await writer.WriteLineAsync(Environment.ProcessId.ToString());
        }
        if (mode == "idle") { await Task.Delay(Timeout.Infinite); return 0; }
        var port = int.Parse(Environment.GetEnvironmentVariable("STREAM_PORT")!);
        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        var unhealthy = false;
        try
        {
            while (true)
            {
                using var client = await listener.AcceptTcpClientAsync();
                try
                {
                    await using var stream = client.GetStream();
                    using var reader = new StreamReader(stream, Encoding.ASCII, leaveOpen: true);
                    var request = await reader.ReadLineAsync();
                    if (request is null) continue;
                    while (!string.IsNullOrEmpty(await reader.ReadLineAsync())) { }
                    var path = request.Split(' ')[1];
                    if (path == "/test-unhealthy") unhealthy = true;
                    var instance = mode == "wrong-id" ? "wrong-id" : Environment.GetEnvironmentVariable("STREAM_INSTANCE_ID") ?? "external";
                    var pid = mode == "wrong-pid" ? 1 : mode == "unrelated-pid" ? int.Parse(Environment.GetEnvironmentVariable("TRAY_TEST_UNRELATED_PID")!) : Environment.ProcessId;
                    var body = mode == "unknown" || unhealthy ? "{}" :
                        path == "/test-details"
                            ? JsonSerializer.Serialize(new { instanceId = instance, pid, token = Environment.GetEnvironmentVariable("STREAM_TOKEN") })
                            : JsonSerializer.Serialize(new { ok = true, application = "LoDeskStream", instanceId = instance, pid });
                    var bytes = Encoding.UTF8.GetBytes(body);
                    var header = Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n");
                    await stream.WriteAsync(header);
                    await stream.WriteAsync(bytes);
                }
                catch (IOException) { }
            }
        }
        finally { listener.Stop(); }
    }
}
