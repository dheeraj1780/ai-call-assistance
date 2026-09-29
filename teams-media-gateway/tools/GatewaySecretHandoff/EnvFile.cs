using System.Text;
using System.Text.RegularExpressions;

namespace CallCopilot.GatewaySecretHandoff;

/// <summary>Failure while editing the .env file. Messages never contain values.</summary>
internal sealed class EnvFileException(string message) : Exception(message);

/// <summary>.env text handling (port of the tested PowerShell logic). Values are supplied through
/// providers so callers never format them into messages.</summary>
internal static class EnvFile
{
    private static readonly byte[] Utf8Bom = [0xEF, 0xBB, 0xBF];

    public static (string Text, bool Bom) Decode(byte[] bytes)
    {
        var bom = bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF;
        var offset = bom ? 3 : 0;
        return (new UTF8Encoding(false, true).GetString(bytes, offset, bytes.Length - offset), bom);
    }

    public static byte[] Encode(string text, bool bom)
    {
        var body = new UTF8Encoding(false).GetBytes(text);
        return bom ? [.. Utf8Bom, .. body] : body;
    }

    private static Regex KeyLine(string name) =>
        new($@"^\s*{Regex.Escape(name)}\s*=", RegexOptions.CultureInvariant);

    private static List<string> SplitLines(string text)
    {
        var lines = text.Length == 0 ? new List<string>() : Regex.Split(text, "\r?\n").ToList();
        if (lines.Count > 0 && lines[^1].Length == 0) lines.RemoveAt(lines.Count - 1); // final newline
        return lines;
    }

    /// <summary>Returns the new text with exactly one "name=value" line: replaced in place, or appended.
    /// Every other line (comments, blanks, order), the line-ending style and a single final newline
    /// are preserved.</summary>
    public static string Update(string text, string name, Func<string> valueProvider, bool allowReplace)
    {
        var newline = text.Contains("\r\n", StringComparison.Ordinal) ? "\r\n" : "\n";
        var lines = SplitLines(text);
        var key = KeyLine(name);
        var indexes = lines.Select((l, i) => (l, i)).Where(t => key.IsMatch(t.l)).Select(t => t.i).ToList();
        if (indexes.Count > 1)
            throw new EnvFileException($"{name} appears {indexes.Count} times in the file; fix it manually first. Nothing changed.");
        if (indexes.Count == 1 && !allowReplace)
            throw new EnvFileException($"{name} already exists; rerun with --replace to rotate it. Nothing changed.");
        var line = name + "=" + valueProvider();
        if (indexes.Count == 1) lines[indexes[0]] = line; else lines.Add(line);
        return string.Join(newline, lines) + newline;
    }

    /// <summary>Every line except "name=...", in order (blank lines and comments included).</summary>
    public static IReadOnlyList<string> LinesExcept(string text, string name)
    {
        var key = KeyLine(name);
        return SplitLines(text).Where(l => !key.IsMatch(l)).ToList();
    }

    /// <summary>Number of "name=..." lines and whether exactly one equals name=expected (ordinal).</summary>
    public static (int Count, bool Matches) KeyState(string text, string name, Func<string> expectedProvider)
    {
        var key = KeyLine(name);
        var keyLines = SplitLines(text).Where(l => key.IsMatch(l)).ToList();
        return (keyLines.Count, keyLines.Count == 1 && string.Equals(keyLines[0], name + "=" + expectedProvider(), StringComparison.Ordinal));
    }

    /// <summary>Writes the bytes to a same-directory temporary file and atomically replaces the target
    /// (File.Replace keeps the target's ACL; no backup copy). The temporary file never survives.</summary>
    public static void AtomicReplace(string target, byte[] content)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(target)) ?? throw new EnvFileException("Invalid path.");
        var tmp = Path.Combine(dir, Path.GetFileName(target) + ".tmp-" + Guid.NewGuid().ToString("N"));
        try
        {
            File.WriteAllBytes(tmp, content);
            File.Replace(tmp, target, destinationBackupFileName: null);
        }
        finally
        {
            if (File.Exists(tmp)) File.Delete(tmp);
        }
    }
}
