using System.Text.RegularExpressions;
using Microsoft.Graph.Models;

namespace CallCopilot.TeamsMediaGateway.Teams;

/// <summary>Safe diagnostic fields of a Teams call termination (Graph <see cref="ResultInfo"/>).</summary>
public static partial class CallDiagnostics
{
    public const int MaxMessageLength = 300;

    [GeneratedRegex(@"https?://\S+", RegexOptions.CultureInvariant | RegexOptions.IgnoreCase)]
    private static partial Regex Url();

    [GeneratedRegex(@"\s+", RegexOptions.CultureInvariant)]
    private static partial Regex Whitespace();

    /// <summary>Code, subcode and message of a result (all null-safe). The message is reduced to a
    /// single line, any URL is replaced by "&lt;url&gt;" (join links can carry meeting parameters) and it
    /// is truncated; "none" when absent.</summary>
    public static (int? Code, int? Subcode, string Message) FromResultInfo(ResultInfo? result)
    {
        var message = result?.Message;
        if (string.IsNullOrWhiteSpace(message)) return (result?.Code, result?.Subcode, "none");
        message = Whitespace().Replace(Url().Replace(message, "<url>"), " ").Trim();
        if (message.Length > MaxMessageLength) message = message[..MaxMessageLength] + "...";
        return (result?.Code, result?.Subcode, message);
    }
}
