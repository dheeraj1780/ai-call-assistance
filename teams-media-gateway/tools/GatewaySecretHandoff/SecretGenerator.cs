using System.Security.Cryptography;

namespace CallCopilot.GatewaySecretHandoff;

internal static class SecretGenerator
{
    public const int ByteLength = 48;
    public const int EncodedLength = 64;

    /// <summary>48 cryptographically random bytes as base64url without padding (exactly 64 characters,
    /// [A-Za-z0-9_-]). The byte buffer is zeroed before returning.</summary>
    public static string NewSecret()
    {
        Span<byte> bytes = stackalloc byte[ByteLength];
        try
        {
            RandomNumberGenerator.Fill(bytes);
            return Convert.ToBase64String(bytes).Replace('+', '-').Replace('/', '_').TrimEnd('=');
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }
}
