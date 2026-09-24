using System.Security.Cryptography;
using System.Text;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>
/// HMAC request signing shared with the CallCopilot API (app/integrations/providers/teams.py):
///   X-CC-Timestamp: unix seconds
///   X-CC-Signature: v1=hex(HMAC-SHA256(secret, "{timestamp}." + body))
/// Requests older than 5 minutes are rejected (replay protection).
/// </summary>
public static class Signing
{
    public const int MaxSkewSeconds = 300;

    public static (string Timestamp, string Signature) Sign(string secret, byte[] body, long? timestamp = null)
    {
        var ts = (timestamp ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds()).ToString();
        using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(secret));
        var payload = Encoding.UTF8.GetBytes(ts + ".").Concat(body).ToArray();
        var digest = Convert.ToHexString(hmac.ComputeHash(payload)).ToLowerInvariant();
        return (ts, "v1=" + digest);
    }

    public static bool Verify(string secret, string? timestamp, string? signature, byte[] body)
    {
        if (string.IsNullOrEmpty(secret) || !long.TryParse(timestamp, out var ts) || signature is null)
            return false;
        if (Math.Abs(DateTimeOffset.UtcNow.ToUnixTimeSeconds() - ts) > MaxSkewSeconds)
            return false;
        var expected = Sign(secret, body, ts).Signature;
        return CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(signature));
    }
}
