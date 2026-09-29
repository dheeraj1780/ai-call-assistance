using System.Diagnostics;

namespace CallCopilot.GatewaySecretHandoff;

/// <summary>
/// Usage (dev PC):
///   GatewaySecretHandoff.exe --env D:\...\backend\.env [--replace] [--clipboard-seconds 180]
///   GatewaySecretHandoff.exe --env %TEMP%\...\.env --dry-run      (throwaway value; exposure scan)
/// Arguments carry only paths and switches, never the value. The value is never printed.
/// </summary>
internal static class Program
{
    internal const string Key = "TEAMS_MEDIA_GATEWAY_SECRET";

    private static string? secret;          // the only copy; cleared (dereferenced) in finally
    private static int guardTrips;          // times an outgoing message had to be redacted (must stay 0)

    /// <summary>Every console line goes through here; a message can never carry the value.</summary>
    private static void Out(string message)
    {
        if (secret is not null && message.Contains(secret, StringComparison.Ordinal))
        {
            guardTrips++;
            message = message.Replace(secret, "<redacted>", StringComparison.Ordinal);
        }
        Console.WriteLine(message);
    }

    [STAThread]
    private static int Main(string[] args)
    {
        string? envArg = null; var replace = false; var dryRun = false; var clipboardSeconds = 180;
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--env" when i + 1 < args.Length: envArg = args[++i]; break;
                case "--replace": replace = true; break;
                case "--dry-run": dryRun = true; break;
                case "--clipboard-seconds" when i + 1 < args.Length && int.TryParse(args[i + 1], out var s) && s is >= 30 and <= 600:
                    clipboardSeconds = s; i++; break;
                default: Out("Usage: GatewaySecretHandoff.exe --env <path-to-.env> [--replace] [--clipboard-seconds 30-600] [--dry-run]"); return 2;
            }
        }
        if (envArg is null) { Out("Missing --env <path>."); return 2; }

        var startUtc = DateTime.UtcNow;
        var envPath = Path.GetFullPath(envArg);
        var ok = false;
        try
        {
            if (dryRun && !envPath.StartsWith(Path.GetFullPath(Path.GetTempPath()), StringComparison.OrdinalIgnoreCase))
                throw new EnvFileException("--dry-run only accepts a .env under %TEMP% (never the real backend/.env). Nothing changed.");
            if (!File.Exists(envPath)) throw new EnvFileException("The .env file does not exist. Nothing changed.");
            if (!IsGitIgnored(envPath)) throw new EnvFileException("The .env file is NOT git-ignored; refusing to write a secret into it. Nothing changed.");
            Out($"mode: {(dryRun ? "DRY RUN (throwaway value, temporary .env)" : "REAL")}; target git-ignored: True");

            var originalBytes = File.ReadAllBytes(envPath);
            var (before, bom) = EnvFile.Decode(originalBytes);
            Array.Clear(originalBytes);

            secret = SecretGenerator.NewSecret();
            var updatedBytes = EnvFile.Encode(EnvFile.Update(before, Key, () => secret!, replace), bom);
            EnvFile.AtomicReplace(envPath, updatedBytes);
            Array.Clear(updatedBytes);

            var checkBytes = File.ReadAllBytes(envPath);
            var (after, bomAfter) = EnvFile.Decode(checkBytes);
            Array.Clear(checkBytes);
            var (count, matches) = EnvFile.KeyState(after, Key, () => secret!);
            var othersSame = EnvFile.LinesExcept(before, Key).SequenceEqual(EnvFile.LinesExcept(after, Key), StringComparer.Ordinal);
            var tempLeft = Directory.EnumerateFiles(Path.GetDirectoryName(envPath)!, Path.GetFileName(envPath) + ".tmp-*").Any();
            Out($".env: {Key} lines={count} stored={matches}; all other lines unchanged={othersSame}; BOM preserved={bomAfter == bom}; temp file left={tempLeft}");
            if (count != 1 || !matches || !othersSame || bomAfter != bom || tempLeft)
                throw new EnvFileException(".env verification failed - inspect the file manually (the value is not shown).");

            var clipboardClean = ClipboardHandoff.HandOff(() => secret!, () =>
            {
                if (dryRun) { Out("clipboard: value placed (dry run: auto-clear in 3 s)"); Thread.Sleep(3000); return; }
                Out("");
                Out("The new secret is on the clipboard (excluded from clipboard history and cloud sync).");
                Out("In the RDP session paste it into BOTH hidden prompts of rotate-backend-secret-on-vm.ps1,");
                Out($"then press Enter here. The clipboard is cleared automatically after {clipboardSeconds} s.");
                WaitForEnter(TimeSpan.FromSeconds(clipboardSeconds));
            });
            Out($"clipboard cleared: {clipboardClean}");
            ok = clipboardClean;

            if (dryRun)
            {
                Thread.Sleep(2000); // let event log writers flush
                ok &= ReportExposureScan(startUtc, envPath);
            }
        }
        catch (EnvFileException ex) { Out("STOPPED: " + ex.Message); }
        catch (Exception ex) { Out($"STOPPED: {ex.GetType().Name}: {ex.Message}"); }
        finally
        {
            try { if (secret is not null && !ClipboardHandoff.ClearIfHolds(() => secret!)) Out("WARNING: could not clear the clipboard - copy any other text now."); }
            catch (Exception) { Out("WARNING: clipboard check failed - copy any other text now."); }
            secret = null;
            GC.Collect();
        }
        if (guardTrips > 0) { Out($"output guard redactions: {guardTrips} (unexpected)"); ok = false; }
        Out(ok ? "RESULT: OK" : "RESULT: FAILED");
        return ok ? 0 : 1;
    }

    private static bool ReportExposureScan(DateTime startUtc, string envPath)
    {
        var logs = ExposureScanner.ScanEventLogs(startUtc, () => secret!);
        foreach (var l in logs) Out($"scan event log '{l.Log}': {(l.Readable ? $"{l.Scanned} events since start, hits={l.Hits}" : "not present/readable")}");
        var procs = ExposureScanner.ScanProcessCommandLines(() => secret!);
        Out($"scan process command lines: {procs.Scanned} readable, {procs.Inaccessible} not readable, hits={procs.Hits}");
        var files = ExposureScanner.ScanFiles(Path.GetTempPath(), startUtc, envPath, () => secret!);
        Out($"scan %TEMP% files written since start (excluding the target .env): {files.Scanned}, hits={files.Hits}");
        foreach (var p in files.HitPaths) Out("   hit: " + p);
        var clipboard = ClipboardHandoff.Holds(() => secret!);
        Out($"scan clipboard holds value: {clipboard}");
        var psLogRead = logs.First(l => l.Log == "Microsoft-Windows-PowerShell/Operational").Readable;
        var clean = psLogRead && logs.All(l => l.Hits == 0) && procs.Hits == 0 && files.Hits == 0 && !clipboard;
        return clean && ControlScan(startUtc, envPath);
    }

    /// <summary>Dry run only: proves the scanners can see a planted, NON-SECRET marker (from the
    /// GSH_CONTROL_MARKER environment variable) in a 4104 event, a process command line and a %TEMP%
    /// file - otherwise "0 hits" would prove nothing.</summary>
    private static bool ControlScan(DateTime startUtc, string envPath)
    {
        var marker = Environment.GetEnvironmentVariable("GSH_CONTROL_MARKER");
        if (string.IsNullOrEmpty(marker) || marker.Length < 24)
        {
            Out("control scan: no GSH_CONTROL_MARKER supplied - scanner sensitivity NOT established");
            return false;
        }
        var logs = ExposureScanner.ScanEventLogs(startUtc, () => marker);
        var ps = logs.First(l => l.Log == "Microsoft-Windows-PowerShell/Operational").Hits;
        var procs = ExposureScanner.ScanProcessCommandLines(() => marker).Hits;
        var files = ExposureScanner.ScanFiles(Path.GetTempPath(), startUtc, envPath, () => marker).Hits;
        Out($"control scan (planted non-secret marker): 4104 log hits={ps}, process command line hits={procs}, %TEMP% file hits={files}");
        var sensitive = ps >= 1 && procs >= 1 && files >= 1;
        Out($"scanner sensitivity established: {sensitive}");
        return sensitive;
    }

    private static bool IsGitIgnored(string envPath)
    {
        // Only the directory and file name are passed to git - never the value.
        var psi = new ProcessStartInfo("git")
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
        };
        psi.ArgumentList.Add("-C"); psi.ArgumentList.Add(Path.GetDirectoryName(envPath)!);
        psi.ArgumentList.Add("check-ignore"); psi.ArgumentList.Add("-q"); psi.ArgumentList.Add("--");
        psi.ArgumentList.Add(Path.GetFileName(envPath));
        try
        {
            using var git = Process.Start(psi) ?? throw new EnvFileException("git could not be started. Nothing changed.");
            git.StandardInput.Close();
            _ = git.StandardOutput.ReadToEnd(); _ = git.StandardError.ReadToEnd();
            if (!git.WaitForExit(30_000)) { git.Kill(); return false; }
            return git.ExitCode == 0;
        }
        catch (System.ComponentModel.Win32Exception) { throw new EnvFileException("git is not on PATH (needed to confirm the .env is git-ignored). Nothing changed."); }
    }

    private static void WaitForEnter(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (!Console.IsInputRedirected && Console.KeyAvailable && Console.ReadKey(intercept: true).Key == ConsoleKey.Enter) return;
            Thread.Sleep(200);
        }
    }
}
