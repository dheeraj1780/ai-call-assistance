using System.Windows.Forms;

namespace CallCopilot.GatewaySecretHandoff;

/// <summary>Clipboard hand-off of the value. Must run on an STA thread ([STAThread] Main).</summary>
internal static class ClipboardHandoff
{
    public const string HistoryFormat = "CanIncludeInClipboardHistory";
    public const string CloudFormat = "CanUploadToCloudClipboard";

    /// <summary>Text plus the two Windows clipboard formats that keep it out of clipboard history and
    /// cloud clipboard sync (DWORD 0 = not allowed).</summary>
    public static DataObject Build(Func<string> valueProvider)
    {
        var data = new DataObject();
        data.SetData(DataFormats.UnicodeText, valueProvider());
        data.SetData(HistoryFormat, new MemoryStream(BitConverter.GetBytes(0)));
        data.SetData(CloudFormat, new MemoryStream(BitConverter.GetBytes(0)));
        return data;
    }

    public static void Place(Func<string> valueProvider) => Retry(() => Clipboard.SetDataObject(Build(valueProvider), copy: true));

    /// <summary>True when the clipboard currently holds text containing the value.</summary>
    public static bool Holds(Func<string> valueProvider)
    {
        var holds = false;
        Retry(() =>
        {
            var text = Clipboard.ContainsText() ? Clipboard.GetText() : null;
            holds = text is not null && text.Contains(valueProvider(), StringComparison.Ordinal);
        });
        return holds;
    }

    /// <summary>Clears the clipboard if (and only if) it still holds the value. Returns true when the
    /// clipboard no longer holds it afterwards.</summary>
    public static bool ClearIfHolds(Func<string> valueProvider)
    {
        if (Holds(valueProvider)) Retry(Clipboard.Clear);
        return !Holds(valueProvider);
    }

    /// <summary>Places the value, runs <paramref name="wait"/>, and always clears it again, including
    /// when <paramref name="wait"/> throws.</summary>
    public static bool HandOff(Func<string> valueProvider, Action wait)
    {
        try
        {
            Place(valueProvider);
            wait();
        }
        finally
        {
            ClearIfHolds(valueProvider);
        }
        return !Holds(valueProvider);
    }

    private static void Retry(Action action)
    {
        // The clipboard can be briefly locked by another process (e.g. rdpclip).
        for (var attempt = 1; ; attempt++)
        {
            try { action(); return; }
            catch (System.Runtime.InteropServices.ExternalException) when (attempt < 10) { Thread.Sleep(100); }
        }
    }
}
