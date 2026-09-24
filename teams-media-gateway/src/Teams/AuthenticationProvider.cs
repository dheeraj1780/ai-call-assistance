using CallCopilot.TeamsMediaGateway.Core;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Identity.Client;

namespace CallCopilot.TeamsMediaGateway.Teams;

/// <summary>
/// App-only Graph tokens for the Communications SDK (MSAL client credentials). Registered with
/// <c>CommunicationsClientBuilder.SetAuthentication(appId, tokenProvider)</c>, which makes the SDK
/// use Microsoft's DefaultAuthenticationProvider: that component also validates the
/// Microsoft-signed JWT on every inbound call-signalling notification (/api/calling).
/// Tokens are never logged.
/// </summary>
public sealed class TokenProvider(GatewayOptions options) : ITokenProvider
{
    private readonly IConfidentialClientApplication app = ConfidentialClientApplicationBuilder
        .Create(options.AppId)
        .WithClientSecret(options.AppSecret)
        .WithAuthority(AzureCloudInstance.AzurePublic, string.IsNullOrEmpty(options.HomeTenantId) ? "organizations" : options.HomeTenantId)
        .Build();

    public async Task<string> AcquireTokenAsync(string tenantId)
    {
        var tenant = string.IsNullOrEmpty(tenantId) ? options.HomeTenantId : tenantId;
        var result = await app.AcquireTokenForClient(["https://graph.microsoft.com/.default"])
            .WithTenantId(tenant).ExecuteAsync().ConfigureAwait(false);
        return result.AccessToken;
    }
}
