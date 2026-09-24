using System.Security.Cryptography;
using System.Text;

namespace CallCopilot.TeamsMediaGateway.Core;

/// <summary>
/// HMAC request signing shared with the CallCopilot API (backend app/integrations/providers/teams.py):
///   X-CC-Timestamp: unix seconds
///   X-CC-Signature: v1=hex(HMAC-SHA256(secret, "{timestamp}." + body))
/// Requests more than 5 minutes old (or in the future) are rejected.
/// </summary>
public static class Signing
{
    public const int MaxSkewSeconds = 300;
    public const string TimestampHeader = "X-CC-Timestamp";
    public const string SignatureHeader = "X-CC-Signature";

    public static (string Timestamp, string Signature) Sign(string secret, ReadOnlySpan<byte> body, long? timestamp = null)
    {
        var ts = (timestamp ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds()).ToString(System.Globalization.CultureInfo.InvariantCulture);
        var prefix = Encoding.UTF8.GetBytes(ts + ".");
        var payload = new byte[prefix.Length + body.Length];
        prefix.CopyTo(payload, 0);
        body.CopyTo(payload.AsSpan(prefix.Length));
        var digest = HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret), payload);
        return (ts, "v1=" + Convert.ToHexString(digest).ToLowerInvariant());
    }

    public static bool Verify(string secret, string? timestamp, string? signature, ReadOnlySpan<byte> body, DateTimeOffset? now = null)
    {
        if (string.IsNullOrEmpty(secret) || signature is null || !long.TryParse(timestamp, out var ts))
            return false;
        var current = (now ?? DateTimeOffset.UtcNow).ToUnixTimeSeconds();
        if (Math.Abs(current - ts) > MaxSkewSeconds)
            return false;
        var expected = Sign(secret, body, ts).Signature;
        return CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(signature));
    }
}
