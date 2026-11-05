using System.Drawing;
using System.Reflection;

internal static class BrandIconTests
{
    internal static Task RunAsync(string? executable = null)
    {
        var assembly = Assembly.Load("LoDeskStreamManager");
        using var resource = assembly.GetManifestResourceStream("LoDeskStream.Brand.ico")
            ?? throw new Exception("Manager is missing its embedded brand icon.");
        using var expected = typeof(BrandIconTests).Assembly.GetManifestResourceStream("BrandIcon.Expected.png")!;
        using var data = new MemoryStream();
        resource.CopyTo(data);
        using var png = new MemoryStream();
        expected.CopyTo(png);
        if (!data.ToArray().AsSpan(22).SequenceEqual(png.ToArray()))
            throw new Exception("The embedded icon does not contain the actual brand PNG.");

        var create = assembly.GetType("LoDeskStream.BrandIcon", throwOnError: true)!
            .GetMethod("Create", BindingFlags.Static | BindingFlags.NonPublic)!;
        var originalDirectory = Environment.CurrentDirectory;
        try
        {
            Environment.CurrentDirectory = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            using var icon = (Icon)create.Invoke(null, null)!;
            using var bitmap = icon.ToBitmap();
            png.Position = 0;
            using var expectedBitmap = new Bitmap(png);
            CheckImage(bitmap, expectedBitmap);
            // A second independent instance must survive disposal of the first.
            using var second = (Icon)create.Invoke(null, null)!;
            icon.Dispose();
            using var secondBitmap = second.ToBitmap();
            CheckImage(secondBitmap, expectedBitmap);
            if (executable != null)
            {
                using var executableIcon = Icon.ExtractAssociatedIcon(executable)
                    ?? throw new Exception("Executable has no icon.");
                using var executableBitmap = executableIcon.ToBitmap();
                // Native extraction can resize; the blue chart and green outline must survive.
                if (!HasBrandColors(executableBitmap))
                    throw new Exception("Executable does not have the LoDeskStream brand colors.");
            }
        }
        finally { Environment.CurrentDirectory = originalDirectory; }
        return Task.CompletedTask;
    }

    private static void CheckImage(Bitmap actual, Bitmap expected)
    {
        if (actual.Size != expected.Size) throw new Exception("Brand icon dimensions changed.");
        for (var y = 0; y < actual.Height; y++)
        for (var x = 0; x < actual.Width; x++)
            if (actual.GetPixel(x, y).ToArgb() != expected.GetPixel(x, y).ToArgb())
                throw new Exception($"Brand icon pixel differs at {x},{y}.");
    }

    private static bool HasBrandColors(Bitmap bitmap)
    {
        var blue = false;
        var green = false;
        for (var y = 0; y < bitmap.Height; y++)
        for (var x = 0; x < bitmap.Width; x++)
        {
            var color = bitmap.GetPixel(x, y);
            blue |= color.B > 150 && color.B > color.R * 1.5 && color.B > color.G;
            green |= color.G > 100 && color.G > color.R * 1.5 && color.G > color.B;
        }
        return blue && green;
    }
}
