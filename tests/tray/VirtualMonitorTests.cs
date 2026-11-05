using System.ComponentModel;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using LoDeskStream;

internal static class VirtualMonitorTests
{
    private static VirtualMonitorDevice Device(int code = 0, string id = @"ROOT\DISPLAY\0001") =>
        new(id, [@"Root\MttVDD"], "MikeTheTech", VirtualMonitorController.SupportedVersion, code);

    private static void Check(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static async Task FailsAsync(Func<Task> run, string message)
    {
        try { await run(); }
        catch (InvalidOperationException) { return; }
        throw new InvalidOperationException(message);
    }

    private static VirtualMonitorCommandResult Probe(params VirtualMonitorDevice[] devices) =>
        new(0, JsonSerializer.Serialize(devices));

    public static async Task RunAsync()
    {
        var good = Device();
        Check(VirtualMonitorController.Evaluate([good]).State == VirtualMonitorState.On, "Enabled driver not recognized.");
        Check(VirtualMonitorController.Evaluate([Device(22)]).State == VirtualMonitorState.Off, "Disabled driver not recognized.");
        foreach (var devices in new VirtualMonitorDevice[][]
        {
            [],
            [good, Device(22, @"ROOT\DISPLAY\0002")],
            [good with { Provider = "Unrelated vendor" }],
            [good with { Version = "99.0.0.0" }],
            [good with { HardwareIds = ["IddSampleDriver"] }],
            [good with { InstanceId = @"PCI\DISPLAY\0001" }],
            [Device(28)],
            [Device(14)],
        })
            Check(!VirtualMonitorController.Evaluate(devices).CanChange, "Unsafe driver accepted.");

        foreach (var result in new[]
        {
            new VirtualMonitorCommandResult(1, "[]"),
            new VirtualMonitorCommandResult(0, "not json"),
            new VirtualMonitorCommandResult(0, "null"),
            new VirtualMonitorCommandResult(0, "[null]"),
            new VirtualMonitorCommandResult(0, "[{}]"),
            new VirtualMonitorCommandResult(0, """[{"InstanceId":null,"HardwareIds":[],"Provider":"MikeTheTech","Version":"11.30.4.434","ProblemCode":0}]"""),
            new VirtualMonitorCommandResult(0, """[{"InstanceId":"ROOT\\DISPLAY\\0001","HardwareIds":["Root\\MttVDD"],"Provider":"MikeTheTech","Version":"11.30.4.434"}]"""),
        })
        {
            var controller = new VirtualMonitorController((_, _) => Task.FromResult(result));
            Check((await controller.RefreshAsync()).State == VirtualMonitorState.Failed, "Probe failure was treated as successful.");
        }
        var brokenProbe = new VirtualMonitorController((_, _) => throw new Win32Exception("Probe missing"));
        var brokenStatus = await brokenProbe.RefreshAsync();
        Check(!brokenStatus.CanChange && brokenStatus.Detail.Contains("Probe missing"),
            "Unavailable PowerShell did not fail closed with its diagnostic.");
        foreach (var exception in new Exception[]
        {
            new IOException("Synthetic pipe read failure"),
            new InvalidOperationException("Synthetic process startup failure"),
            new JsonException("Synthetic invalid probe JSON"),
            new TimeoutException("Synthetic probe timeout"),
            new UnauthorizedAccessException("Synthetic probe access denied"),
        })
        {
            var controller = new VirtualMonitorController((_, _) => throw exception);
            var status = await controller.RefreshAsync();
            Check(status.State == VirtualMonitorState.Failed && status.Detail.Contains(exception.Message),
                $"Expected probe failure lost its diagnostic: {exception.GetType().Name}.");
        }
        var unexpected = new VirtualMonitorController((_, _) => throw new NotSupportedException("Synthetic programming error"));
        try
        {
            await unexpected.RefreshAsync();
            throw new InvalidOperationException("Unexpected programming errors must not be hidden as unavailable drivers.");
        }
        catch (NotSupportedException) { }
        var diagnostic = new VirtualMonitorController((_, _) => Task.FromResult(
            new VirtualMonitorCommandResult(7, "Do not echo arbitrary stdout", "Access denied\r\nby Windows PnP.\t" + new string('x', 1000))));
        var diagnosticStatus = await diagnostic.RefreshAsync();
        Check(diagnosticStatus.Detail.Contains("exit 7") && diagnosticStatus.Detail.Contains("Access denied by Windows PnP.") &&
            !diagnosticStatus.Detail.Contains("arbitrary stdout") && diagnosticStatus.Detail.Length < 700 &&
            !diagnosticStatus.Detail.Any(char.IsControl), "Probe diagnostics were lost, unbounded, or copied from raw stdout.");

        await ReadOnlyProbeDiagnosticsAsync();

        foreach (var enabled in new[] { true, false })
        {
            var commands = new List<(string Script, bool Elevated)>();
            var before = Device(enabled ? 22 : 0);
            var after = Device(enabled ? 0 : 22);
            var results = new Queue<VirtualMonitorCommandResult>([Probe(before), new(0, ""), Probe(after)]);
            var controller = new VirtualMonitorController((script, elevated) =>
            {
                commands.Add((script, elevated));
                return Task.FromResult(results.Dequeue());
            }, () => Task.CompletedTask);
            var status = await controller.ChangeAsync(VirtualMonitorController.Evaluate([before]), enabled);
            Check(status.State == (enabled ? VirtualMonitorState.On : VirtualMonitorState.Off), "State not confirmed.");
            Check(commands.Count == 3 && !commands[0].Elevated && commands[1].Elevated && !commands[2].Elevated,
                "Only the mutation should elevate.");
            Check(commands[1].Script.Contains(enabled ? "Enable-PnpDevice" : "Disable-PnpDevice") &&
                commands[1].Script.Contains("$devices.Count -ne 1") &&
                commands[1].Script.Contains("$device.Provider -ine 'MikeTheTech'") &&
                commands[1].Script.Contains($"$device.Version -ne '{VirtualMonitorController.SupportedVersion}'") &&
                commands[1].Script.Contains("-InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop"),
                "Elevated helper lost its narrow revalidation policy.");
            Check(results.Count == 0, "Successful process exit was not followed by a Windows status probe.");
        }

        var expected = VirtualMonitorController.Evaluate([good]);
        var elevations = 0;
        var changed = new VirtualMonitorController((_, elevated) =>
        {
            if (elevated) elevations++;
            return Task.FromResult(Probe(Device(0, @"ROOT\DISPLAY\0002")));
        });
        await FailsAsync(() => changed.ChangeAsync(expected, false), "Changed device was controlled.");
        await FailsAsync(() => changed.ChangeAsync(new(VirtualMonitorState.Failed, "unknown"), false),
            "Unavailable device was controlled.");
        Check(elevations == 0, "Unsafe or changed identity triggered elevation.");

        foreach (var exit in new[] { 10, 11, 1 })
        {
            var failed = new VirtualMonitorController((_, elevated) =>
                Task.FromResult(elevated ? new VirtualMonitorCommandResult(exit, "") : Probe(good)));
            await FailsAsync(() => failed.ChangeAsync(expected, false), "Helper exit failure reported success.");
        }
        var cancelled = new VirtualMonitorController((_, elevated) => elevated
            ? throw new InvalidOperationException("Administrator approval was cancelled.")
            : Task.FromResult(Probe(good)));
        await FailsAsync(() => cancelled.ChangeAsync(expected, false), "Cancelled UAC reported success.");

        var stale = new VirtualMonitorController((_, elevated) =>
            Task.FromResult(elevated ? new VirtualMonitorCommandResult(0, "") : Probe(good)),
            () => Task.CompletedTask);
        await FailsAsync(() => stale.ChangeAsync(expected, false), "Unchanged Windows state reported success.");

        foreach (var elevated in new[] { false, true })
        {
            var info = VirtualMonitorController.CreateStartInfo("exit 0", elevated);
            Check(info.UseShellExecute == elevated && (info.Verb == "runas") == elevated,
                "Incorrect elevation configuration.");
            Check(info.FileName.StartsWith(Environment.GetFolderPath(Environment.SpecialFolder.System),
                StringComparison.OrdinalIgnoreCase), "Helper uses a PATH-resolved executable.");
            Check(!info.Arguments.Contains("Bypass"), "Helper must not bypass execution policy.");
        }

        // Mutation scripts are parsed only; no test executes a device mutation or requests elevation.
        foreach (var script in new[]
        {
            VirtualMonitorController.ProbeScript,
            VirtualMonitorController.ChangeScript(@"ROOT\DISPLAY\0001", true),
            VirtualMonitorController.ChangeScript(@"ROOT\DISPLAY\0001", false),
            VirtualMonitorController.ChangeScript("ROOT\\DISPLAY\\x'; exit 0; #", false),
        })
        {
            var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
            var parse = "$script = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('" + encoded + "')); " +
                "$tokens = $null; $errors = $null; " +
                "$null = [System.Management.Automation.Language.Parser]::ParseInput($script, [ref]$tokens, [ref]$errors); " +
                "if ($errors.Count -ne 0) { $errors | Out-String | Write-Output; exit 1 }; exit 0";
            using var process = Process.Start(VirtualMonitorController.CreateStartInfo(parse, false))!;
            var output = process.StandardOutput.ReadToEndAsync();
            var errors = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(20));
            Check(process.ExitCode == 0, $"PowerShell script did not parse: {await output} {await errors}");
        }
        Check(VirtualMonitorController.ChangeScript("ROOT\\DISPLAY\\x'; exit 0; #", false)
            .Contains("'ROOT\\DISPLAY\\x''; exit 0; #'"), "Instance ID was not escaped as a literal.");
        Check(VirtualMonitorController.InstallationInstructions.Contains("does not install drivers or certificates") &&
            VirtualMonitorController.InstallationInstructions.Contains("do not disable signature enforcement"),
            "Manual installation UX lost its trust safeguards.");
    }

    private static async Task ReadOnlyProbeDiagnosticsAsync()
    {
        const string query = "Get-PnpDevice -Class Display -PresentOnly -ErrorAction Stop";
        foreach (var emptyQuery in new[]
        {
            "Get-PnpDevice -Class 'LoDeskStream-No-Such-Display-Class' -PresentOnly -ErrorAction Stop",
            "Get-PnpDevice -Class Display -PresentOnly -InstanceId 'ROOT\\LODESKSTREAM_READONLY_PROBE\\NO_SUCH_DEVICE' -ErrorAction Stop",
        })
        {
            var result = await VirtualMonitorController.RunPowerShellAsync(
                VirtualMonitorController.ProbeScript.Replace(query, emptyQuery), false);
            Check(result.ExitCode == 0 && result.Output.Trim() == "[]",
                $"A PnP no-match query must return an empty probe, not a failure: {result.Error}");
            var controller = new VirtualMonitorController((_, _) => Task.FromResult(result));
            Check((await controller.RefreshAsync()).State == VirtualMonitorState.Unavailable,
                "Zero present Display devices must leave On/Off unavailable.");
        }

        foreach (var (script, expectedDetail) in new[]
        {
            (VirtualMonitorController.ProbeScript.Replace(query,
                "& { throw [System.InvalidOperationException]::new('Synthetic PnP enumeration denied') }"),
                "Synthetic PnP enumeration denied"),
            (VirtualMonitorController.ProbeScript.Replace(
                @"$PSHOME\Modules\PnpDevice\PnpDevice.psd1",
                @"$PSHOME\Modules\LoDeskStream-NoSuchProbeModule\Missing.psd1"),
                "LoDeskStream-NoSuchProbeModule"),
        })
        {
            var result = await VirtualMonitorController.RunPowerShellAsync(script, false);
            Check(result.ExitCode == 1 && result.Error.Contains(expectedDetail) &&
                result.Error.Contains("Windows PnP probe:"), "PowerShell probe stderr did not retain the actual failure.");
            var controller = new VirtualMonitorController((_, _) => Task.FromResult(result));
            var status = await controller.RefreshAsync();
            Check(status.State == VirtualMonitorState.Failed && status.Detail.Contains(expectedDetail),
                "Real PowerShell stderr did not reach the failed tray status.");
        }

        var stderr = await VirtualMonitorController.RunPowerShellAsync(
            "[Console]::Error.WriteLine('Synthetic helper stderr'); exit 7", false);
        Check(stderr.ExitCode == 7 && stderr.Error.Contains("Synthetic helper stderr"),
            "Read-only helper stderr was discarded.");
    }
}
