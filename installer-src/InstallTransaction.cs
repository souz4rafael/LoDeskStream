namespace LoDeskStream.Installation;

internal static class InstallTransaction
{
    public static void Execute(string target, string staging, Action commitMetadata, Action rollbackMetadata)
    {
        var parent = Path.GetDirectoryName(target) ?? throw new ArgumentException("Install folder needs a parent.");
        var backup = Path.Combine(parent, $".LoDeskStream-backup-{Guid.NewGuid():N}");
        var failed = Path.Combine(parent, $".LoDeskStream-failed-{Guid.NewGuid():N}");
        var backedUp = false;
        var installed = false;
        var metadataStarted = false;
        try
        {
            if (Directory.Exists(target))
            {
                Directory.Move(target, backup);
                backedUp = true;
            }
            Directory.Move(staging, target);
            installed = true;
            metadataStarted = true;
            commitMetadata();
        }
        catch (Exception original)
        {
            // Metadata can fail after file replacement; restore both parts before reporting failure.
            var failures = new List<Exception> { original };
            try
            {
                if (installed) Directory.Move(target, failed);
                if (backedUp) Directory.Move(backup, target);
            }
            catch (Exception rollbackError) { failures.Add(rollbackError); }
            if (metadataStarted)
            {
                try { rollbackMetadata(); }
                catch (Exception rollbackError) { failures.Add(rollbackError); }
            }
            Cleanup(failed);
            if (failures.Count > 1)
                throw new AggregateException($"Installation and rollback failed. Recovery backup: {backup}", failures);
            throw;
        }
        Cleanup(backup);
    }

    public static void Cleanup(string directory)
    {
        if (!Directory.Exists(directory)) return;
        try { Directory.Delete(directory, recursive: true); }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException)
        {
            Console.Error.WriteLine($"Cleanup incomplete; retained folder: {directory}. {exc.Message}");
        }
    }
}
