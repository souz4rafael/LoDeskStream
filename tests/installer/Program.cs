using LoDeskStream.Installation;
using System.Security.Cryptography;
using System.Text.Json;

var root = Path.Combine(Path.GetTempPath(), $"LoDeskStream-installer-tests-{Guid.NewGuid():N}");
Directory.CreateDirectory(root);
var count = 0;
var version = typeof(InstallTransaction).Assembly.GetName().Version!.ToString(3);
try
{
    Run("successful update", (target, staging) =>
    {
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(target, "version"), "old");
        InstallTransaction.Execute(target, staging, () => { }, () => throw new Exception("Unexpected rollback"));
        Check(File.ReadAllText(Path.Combine(target, "version")) == "new", "New installation missing");
    });
    Run("failed update restores original", (target, staging) =>
    {
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(target, "version"), "old");
        var restored = false;
        ExpectFailure(() => InstallTransaction.Execute(target, staging,
            () => throw new IOException("Metadata failed"), () => restored = true));
        Check(File.ReadAllText(Path.Combine(target, "version")) == "old", "Old installation was lost");
        Check(restored, "Metadata was not restored");
    });
    Run("failed first install leaves no partial target", (target, staging) =>
    {
        ExpectFailure(() => InstallTransaction.Execute(target, staging,
            () => throw new IOException("Metadata failed"), () => { }));
        Check(!Directory.Exists(target), "Partial installation remains");
    });
    Run("staging failure does not alter original", (target, staging) =>
    {
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(target, "version"), "old");
        Directory.Delete(staging, true);
        ExpectFailure(() => InstallTransaction.Execute(target, staging, () => { }, () => { }));
        Check(File.ReadAllText(Path.Combine(target, "version")) == "old", "Original not restored after move failure");
    });
    Run("rollback error is surfaced", (target, staging) =>
    {
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(target, "version"), "old");
        try
        {
            InstallTransaction.Execute(target, staging, () => throw new IOException("Commit failed"),
                () => throw new IOException("Restore failed"));
            throw new Exception("Expected rollback failure");
        }
        catch (AggregateException error) { Check(error.InnerExceptions.Count == 2, "Rollback diagnostics lost"); }
        Check(File.ReadAllText(Path.Combine(target, "version")) == "old", "Files were not restored");
    });
    foreach (var mutation in new[] { "valid", "tampered", "missing", "extra", "traversal", "wrong-version", "null-entry" })
    {
        var payload = Path.Combine(root, "payload-" + mutation);
        Directory.CreateDirectory(payload);
        var files = new List<object?>();
        foreach (var path in PayloadValidator.RequiredFiles)
        {
            var file = Path.Combine(payload, path);
            Directory.CreateDirectory(Path.GetDirectoryName(file)!);
            File.WriteAllText(file, "fixture");
            files.Add(new { path, sha256 = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(file))), size = new FileInfo(file).Length });
        }
        if (mutation == "null-entry") files.Add(null);
        if (mutation == "traversal") files.Add(new { path = @"..\outside", sha256 = "", size = 0 });
        File.WriteAllText(Path.Combine(payload, "payload-manifest.json"), JsonSerializer.Serialize(new {
            application = "LoDeskStream", version = mutation == "wrong-version" ? "0.0.0" : version, files
        }));
        if (mutation == "tampered") File.WriteAllText(Path.Combine(payload, PayloadValidator.RequiredFiles[0]), "changed");
        if (mutation == "missing") File.Delete(Path.Combine(payload, PayloadValidator.RequiredFiles[0]));
        if (mutation == "extra") File.WriteAllText(Path.Combine(payload, "unexpected.txt"), "extra");
        if (mutation == "valid") PayloadValidator.Validate(payload, version);
        else ExpectFailure(() => PayloadValidator.Validate(payload, version));
        count++;
    }
    Console.WriteLine($"{count} installer transaction/payload scenarios passed.");
    return 0;
}
finally { Directory.Delete(root, true); }

void Run(string name, Action<string, string> action)
{
    var scenario = Path.Combine(root, count.ToString());
    var staging = Path.Combine(scenario, "staging");
    var target = Path.Combine(scenario, "install");
    Directory.CreateDirectory(staging);
    File.WriteAllText(Path.Combine(staging, "version"), "new");
    action(target, staging);
    count++;
}

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

static void ExpectFailure(Action action)
{
    try { action(); }
    catch (Exception error) when (error is IOException or InvalidDataException) { return; }
    throw new Exception("Expected installation failure");
}
