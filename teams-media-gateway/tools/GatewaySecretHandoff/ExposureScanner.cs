using System.ComponentModel;
using System.Diagnostics;
using System.Diagnostics.Eventing.Reader;
using System.Runtime.InteropServices;
using System.Text;

namespace CallCopilot.GatewaySecretHandoff;

/// <summary>--dry-run only: looks for the (throwaway) value in places it must never reach. Runs in-process
/// so the value never leaves this process; reports counts only.</summary>
internal static class ExposureScanner
{
    public static readonly string[] EventLogs =
    [
        "Microsoft-Windows-PowerShell/Operational", // 4104 script block logging
        "Windows PowerShell",                       // 400/403/600/800 (HostApplication command lines)
        "Microsoft-Windows-PowerShell/Admin",
        "PowerShellCore/Operational",
        "Application",
        "System",
    ];

    public sealed record LogResult(string Log, bool Readable, int Scanned, int Hits);

    public static List<LogResult> ScanEventLogs(DateTime sinceUtc, Func<string> valueProvider)
    {
        var results = new List<LogResult>();
        var since = sinceUtc.AddSeconds(-5).ToString("yyyy-MM-ddTHH:mm:ss.fffZ", System.Globalization.CultureInfo.InvariantCulture);
        foreach (var log in EventLogs)
        {
            int scanned = 0, hits = 0;
            try
            {
                var query = new EventLogQuery(log, PathType.LogName, $"*[System[TimeCreated[@SystemTime>='{since}']]]");
                using var reader = new EventLogReader(query);
                for (var record = reader.ReadEvent(); record is not null; record = reader.ReadEvent())
                {
                    using (record)
                    {
                        scanned++;
                        var sb = new StringBuilder();
                        try { sb.Append(record.FormatDescription()); } catch (EventLogException) { }
                        foreach (var p in record.Properties) sb.Append('\u0001').Append(p.Value?.ToString());
                        if (sb.ToString().Contains(valueProvider(), StringComparison.Ordinal)) hits++;
                    }
                }
                results.Add(new LogResult(log, true, scanned, hits));
            }
            catch (Exception ex) when (ex is EventLogNotFoundException or EventLogException or UnauthorizedAccessException)
            {
                results.Add(new LogResult(log, false, scanned, hits));
            }
        }
        return results;
    }

    public sealed record ProcessResult(int Scanned, int Inaccessible, int Hits);

    public static ProcessResult ScanProcessCommandLines(Func<string> valueProvider)
    {
        int scanned = 0, inaccessible = 0, hits = 0;
        foreach (var p in Process.GetProcesses())
        {
            using (p)
            {
                var cmd = TryGetCommandLine(p.Id);
                if (cmd is null) { inaccessible++; continue; }
                scanned++;
                if (cmd.Contains(valueProvider(), StringComparison.Ordinal)) hits++;
            }
        }
        return new ProcessResult(scanned, inaccessible, hits);
    }

    public sealed record FileResult(int Scanned, int Hits, IReadOnlyList<string> HitPaths);

    /// <summary>Files under <paramref name="root"/> written since <paramref name="sinceUtc"/>, except the
    /// intended target file. Searched as UTF-8 and UTF-16LE.</summary>
    public static FileResult ScanFiles(string root, DateTime sinceUtc, string excludedFile, Func<string> valueProvider)
    {
        int scanned = 0;
        var hitPaths = new List<string>();
        var excluded = Path.GetFullPath(excludedFile);
        var options = new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true, AttributesToSkip = 0 };
        foreach (var path in Directory.EnumerateFiles(root, "*", options))
        {
            try
            {
                var info = new FileInfo(path);
                if (info.LastWriteTimeUtc < sinceUtc.AddSeconds(-5) || info.Length > 50 * 1024 * 1024) continue;
                if (string.Equals(info.FullName, excluded, StringComparison.OrdinalIgnoreCase)) continue;
                var bytes = File.ReadAllBytes(path);
                scanned++;
                var value = valueProvider();
                if (Encoding.UTF8.GetString(bytes).Contains(value, StringComparison.Ordinal) ||
                    Encoding.Unicode.GetString(bytes).Contains(value, StringComparison.Ordinal))
                    hitPaths.Add(path);
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        }
        return new FileResult(scanned, hitPaths.Count, hitPaths);
    }

    // ---- process command line (ProcessCommandLineInformation = 60, Windows 8.1+) -------------------
    private const int ProcessCommandLineInformation = 60;
    private const uint ProcessQueryLimitedInformation = 0x1000;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint desiredAccess, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr process, int infoClass, IntPtr info, int infoLength, out int returnLength);

    private static string? TryGetCommandLine(int pid)
    {
        var handle = OpenProcess(ProcessQueryLimitedInformation, false, pid);
        if (handle == IntPtr.Zero) return null;
        try
        {
            NtQueryInformationProcess(handle, ProcessCommandLineInformation, IntPtr.Zero, 0, out var needed);
            if (needed <= 0) return null;
            var buffer = Marshal.AllocHGlobal(needed);
            try
            {
                if (NtQueryInformationProcess(handle, ProcessCommandLineInformation, buffer, needed, out _) != 0) return null;
                // UNICODE_STRING { USHORT Length; USHORT MaximumLength; PWSTR Buffer; } followed by the text.
                var length = (ushort)Marshal.ReadInt16(buffer);
                var text = Marshal.ReadIntPtr(buffer, IntPtr.Size);
                return length == 0 || text == IntPtr.Zero ? string.Empty : Marshal.PtrToStringUni(text, length / 2);
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        catch (Win32Exception) { return null; }
        finally { CloseHandle(handle); }
    }
}
