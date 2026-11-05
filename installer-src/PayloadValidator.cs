using System.Security.Cryptography;
using System.Text.Json;

namespace LoDeskStream.Installation;

internal static class PayloadValidator
{
    private const string ManifestName = "payload-manifest.json";
    private static readonly JsonSerializerOptions Options = new() { PropertyNameCaseInsensitive = true };
    internal static readonly string[] RequiredFiles = [
        "LoDeskStream.exe", @"manager\LoDeskStreamManager.exe", @"_internal\web\index.html",
        @"_internal\web\input-queue.js", @"_internal\web\keyboard-input.js", @"_internal\build-info.json"
    ];

    public static void Validate(string root, string version)
    {
        PayloadManifest? manifest;
        try
        {
            manifest = JsonSerializer.Deserialize<PayloadManifest>(
                File.ReadAllText(Path.Combine(root, ManifestName)), Options);
        }
        catch (JsonException exc) { throw new InvalidDataException("Invalid payload manifest JSON.", exc); }
        if (manifest is null || manifest.Application != "LoDeskStream" || manifest.Version != version ||
            manifest.Files is null || manifest.Files.Count == 0)
            throw new InvalidDataException("Payload identity or version does not match the installer.");
        var expected = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in manifest.Files)
        {
            var relative = entry?.Path?.Replace('/', '\\') ?? "";
            if (entry is null || Path.IsPathFullyQualified(relative) || relative.Contains(':') ||
                relative.Split('\\').Any(part => part is "" or "." or "..") ||
                string.Equals(relative, ManifestName, StringComparison.OrdinalIgnoreCase) || !expected.Add(relative))
                throw new InvalidDataException("Unsafe or duplicate payload path.");
            var path = Path.Combine(root, relative);
            var info = new FileInfo(path);
            if (!info.Exists || info.Length != entry.Size || (info.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException($"Missing or invalid payload file: {relative}");
            using var stream = File.OpenRead(path);
            var hash = Convert.ToHexString(SHA256.HashData(stream));
            if (!string.Equals(hash, entry.Sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException($"Payload hash mismatch: {relative}");
        }
        var actual = Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)
            .Select(path => Path.GetRelativePath(root, path))
            .Where(path => !string.Equals(path, ManifestName, StringComparison.OrdinalIgnoreCase));
        if (!expected.SetEquals(actual)) throw new InvalidDataException("Payload contains unexpected files.");
        foreach (var required in RequiredFiles)
            if (!expected.Contains(required)) throw new InvalidDataException($"Payload is incomplete: {required}");
    }

    private sealed record PayloadManifest(string Application, string Version, List<PayloadFile?> Files);
    private sealed record PayloadFile(string Path, string Sha256, long Size);
}
