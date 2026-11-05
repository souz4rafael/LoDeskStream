using System.ComponentModel;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace LoDeskStream;

internal enum VirtualMonitorState { Unavailable, On, Off, Failed }

internal sealed record VirtualMonitorStatus(VirtualMonitorState State, string Detail, string? InstanceId = null)
{
    public bool CanChange => State is VirtualMonitorState.On or VirtualMonitorState.Off;
}

internal sealed record VirtualMonitorDevice(
    [property: JsonRequired] string InstanceId,
    [property: JsonRequired] string[] HardwareIds,
    [property: JsonRequired] string Provider,
    [property: JsonRequired] string Version,
    [property: JsonRequired] int ProblemCode);
internal sealed record VirtualMonitorCommandResult(int ExitCode, string Output, string Error = "");

internal sealed class VirtualMonitorController
{
    internal const string ReleaseUrl = "https://github.com/VirtualDrivers/Virtual-Display-Driver/releases/tag/25.7.23";
    internal const string SupportedVersion = "11.30.4.434";
    internal const string InstallationInstructions =
        "Optional, manual driver-only installation (administrator approval required).\n\n" +
        "LoDeskStream does not install drivers or certificates. Open the upstream 25.7.23 release and follow its driver-only instructions. " +
        "Do not install VDD Control or an extra control app. For Windows x64, the asset named VirtualDisplayDriver-x86.Driver.Only.zip " +
        "actually contains an NTamd64 INF (MttVDD.inf, provider MikeTheTech, version 11.30.4.434). " +
        "ARM64 requires the separate ARM64 driver; LoDeskStream's tray build is currently supported on x64 only.\n\n" +
        "Review the publisher, package and architecture before installing. If Windows rejects the package's signature, stop; " +
        "do not disable signature enforcement or import certificates just to bypass that warning. " +
        "Automatic installation and package trust have not been verified. A download or INF staged in the driver store alone " +
        "does not create a display device; complete the upstream root-device installation procedure, then reopen this tray menu.\n\n" +
        "The tray controls one existing compatible driver device, including all displays it creates; it does not claim ownership " +
        "or change driver configuration. Other apps may use that same device. Multiple matching devices are refused.\n\n" +
        "Open the upstream release page now? No download or installation will be started by LoDeskStream.";

    // Both the read-only probe and elevated operation use this same, narrow identity policy.
    internal const string DeviceScript = """
        $ErrorActionPreference = 'Stop'
        function Read-VirtualDevices {
            Import-Module "$PSHOME\Modules\PnpDevice\PnpDevice.psd1" -ErrorAction Stop
            try {
                $displayDevices = @(Get-PnpDevice -Class Display -PresentOnly -ErrorAction Stop)
            } catch {
                if ($_.FullyQualifiedErrorId -eq 'CmdletizationQuery_NotFound,Get-PnpDevice' -and
                    $_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound) {
                    return
                }
                throw
            }
            foreach ($device in $displayDevices) {
                $id = $device.InstanceId
                $hardware = @((Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_HardwareIds' -ErrorAction Stop).Data)
                if (($hardware -contains 'Root\MttVDD') -or ($hardware -contains 'MttVDD')) {
                    [pscustomobject]@{
                        InstanceId = $id
                        HardwareIds = $hardware
                        Provider = [string](Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_DriverProvider' -ErrorAction Stop).Data
                        Version = [string](Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_DriverVersion' -ErrorAction Stop).Data
                        ProblemCode = [int](Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop).Data
                    }
                }
            }
        }
        """;
    internal static string ProbeScript => DeviceScript + """

        try {
            $devices = @(Read-VirtualDevices)
            ConvertTo-Json -InputObject $devices -Depth 4 -Compress
            exit 0
        } catch {
            [Console]::Error.WriteLine("Windows PnP probe: {0} [{1}]", $_.Exception.Message, $_.FullyQualifiedErrorId)
            exit 1
        }
        """;

    private readonly Func<string, bool, Task<VirtualMonitorCommandResult>> _run;
    private readonly Func<Task> _delay;

    public VirtualMonitorController(
        Func<string, bool, Task<VirtualMonitorCommandResult>>? run = null, Func<Task>? delay = null)
    {
        _run = run ?? RunPowerShellAsync;
        _delay = delay ?? (() => Task.Delay(1000));
    }

    internal static VirtualMonitorStatus Evaluate(IReadOnlyList<VirtualMonitorDevice> devices)
    {
        if (devices.Count == 0)
            return new(VirtualMonitorState.Unavailable, "Compatible driver not installed; use Install Virtual Monitor…");
        if (devices.Count != 1)
            return new(VirtualMonitorState.Unavailable, "Multiple matching driver devices; no device will be controlled");
        var device = devices[0];
        if (!device.InstanceId.StartsWith(@"ROOT\", StringComparison.OrdinalIgnoreCase) ||
            !device.HardwareIds.Any(id => id.Equals(@"Root\MttVDD", StringComparison.OrdinalIgnoreCase) ||
                id.Equals("MttVDD", StringComparison.OrdinalIgnoreCase)) ||
            !device.Provider.Equals("MikeTheTech", StringComparison.OrdinalIgnoreCase) ||
            device.Version != SupportedVersion)
            return new(VirtualMonitorState.Unavailable, "Unsupported driver identity/version; no device will be controlled");
        return device.ProblemCode switch
        {
            0 => new(VirtualMonitorState.On, "Compatible driver enabled (Windows display layout may still disable its screens)", device.InstanceId),
            22 => new(VirtualMonitorState.Off, "Compatible driver disabled", device.InstanceId),
            _ => new(VirtualMonitorState.Unavailable, $"Driver needs attention (Windows problem code {device.ProblemCode}); use Device Manager"),
        };
    }

    public async Task<VirtualMonitorStatus> RefreshAsync()
    {
        try
        {
            var result = await _run(ProbeScript, false);
            if (result.ExitCode != 0)
                throw new InvalidOperationException($"Windows PnP probe failed (exit {result.ExitCode}): " +
                    (string.IsNullOrWhiteSpace(result.Error) ? "No error detail was returned." : result.Error));
            var devices = JsonSerializer.Deserialize<VirtualMonitorDevice[]>(result.Output)
                ?? throw new InvalidOperationException("Empty Windows PnP probe response");
            if (devices.Any(device => device is null || string.IsNullOrWhiteSpace(device.InstanceId) ||
                device.HardwareIds is null || device.HardwareIds.Any(id => id is null) ||
                device.Provider is null || device.Version is null))
                throw new InvalidOperationException("Windows PnP probe response contains missing or invalid device fields.");
            return Evaluate(devices);
        }
        catch (Exception exc) when (exc is IOException or Win32Exception or InvalidOperationException or
            JsonException or TimeoutException or UnauthorizedAccessException)
        {
            var detail = string.Join(" ", exc.Message.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
            detail = new string(detail.Where(character => !char.IsControl(character)).Take(600).ToArray());
            return new(VirtualMonitorState.Failed, $"Driver status unavailable; On/Off disabled. {detail}");
        }
    }

    internal static string ChangeScript(string instanceId, bool enabled)
    {
        var literal = "'" + instanceId.Replace("'", "''") + "'";
        return DeviceScript + $$"""

            try {
                $devices = @(Read-VirtualDevices)
                if ($devices.Count -ne 1) { exit 10 }
                $device = $devices[0]
                if ($device.InstanceId -ine {{literal}} -or
                    -not $device.InstanceId.StartsWith('ROOT\', [System.StringComparison]::OrdinalIgnoreCase) -or
                    $device.Provider -ine 'MikeTheTech' -or $device.Version -ne '{{SupportedVersion}}' -or
                    $device.ProblemCode -ne {{(enabled ? 22 : 0)}}) { exit 10 }
                {{(enabled ? "Enable" : "Disable")}}-PnpDevice -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
                exit 0
            } catch { exit 11 }
            """;
    }

    public async Task<VirtualMonitorStatus> ChangeAsync(VirtualMonitorStatus expected, bool enabled)
    {
        if (!expected.CanChange || expected.InstanceId is null)
            throw new InvalidOperationException("No single compatible driver is available.");
        var current = await RefreshAsync();
        if (!current.CanChange || current.InstanceId != expected.InstanceId || current.State != expected.State)
            throw new InvalidOperationException("Driver status changed. Reopen the tray menu and try again.");
        if ((current.State == VirtualMonitorState.On) == enabled)
            throw new InvalidOperationException("The driver is already in the requested state.");

        var result = await _run(ChangeScript(current.InstanceId!, enabled), true);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"Virtual monitor operation failed (helper exit {result.ExitCode}). No success was confirmed; reopen the menu to check Windows status.");
        for (var attempt = 0; attempt < 6; attempt++)
        {
            current = await RefreshAsync();
            if (current.InstanceId == expected.InstanceId &&
                current.State == (enabled ? VirtualMonitorState.On : VirtualMonitorState.Off)) return current;
            if (attempt < 5) await _delay();
        }
        throw new InvalidOperationException("Windows did not confirm the requested driver state. A restart or Device Manager check may be required.");
    }

    internal static ProcessStartInfo CreateStartInfo(string script, bool elevated)
    {
        var info = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe"))
        {
            UseShellExecute = elevated,
            Verb = elevated ? "runas" : "",
            CreateNoWindow = !elevated,
            WindowStyle = ProcessWindowStyle.Hidden,
            RedirectStandardOutput = !elevated,
            RedirectStandardError = !elevated,
        };
        info.Arguments = "-NoLogo -NoProfile -NonInteractive -EncodedCommand " +
            Convert.ToBase64String(Encoding.Unicode.GetBytes(script));
        return info;
    }

    internal static async Task<VirtualMonitorCommandResult> RunPowerShellAsync(string script, bool elevated)
    {
        try
        {
            using var process = Process.Start(CreateStartInfo(script, elevated))
                ?? throw new InvalidOperationException("Could not start the Windows PnP helper.");
            var output = elevated ? Task.FromResult("") : process.StandardOutput.ReadToEndAsync();
            var error = elevated ? Task.FromResult("") : process.StandardError.ReadToEndAsync();
            if (elevated)
                await process.WaitForExitAsync(); // Never abandon an elevated mutation while it might still run.
            else
            {
                try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(20)); }
                catch (TimeoutException exc)
                {
                    try { process.Kill(); } catch (InvalidOperationException) { }
                    throw new TimeoutException("Windows PnP probe timed out after 20 seconds.", exc);
                }
            }
            return new(process.ExitCode, await output, await error);
        }
        catch (Win32Exception exc) when (exc.NativeErrorCode == 1223)
        {
            throw new InvalidOperationException("Administrator approval was cancelled. No driver change was requested.", exc);
        }
    }
}
