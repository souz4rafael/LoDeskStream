using System.Drawing;

namespace LoDeskStream;

internal static class BrandIcon
{
    internal static Icon Create()
    {
        using var stream = typeof(BrandIcon).Assembly.GetManifestResourceStream("LoDeskStream.Brand.ico")
            ?? throw new InvalidOperationException("The embedded LoDeskStream icon is missing.");
        using var icon = new Icon(stream);
        // The returned icon owns its image independently of the resource stream.
        return (Icon)icon.Clone();
    }
}
