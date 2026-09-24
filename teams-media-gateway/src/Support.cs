using System.Net.Http.Headers;
using System.Text.RegularExpressions;
using System.Web;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common;
using Microsoft.Graph.Models;
using Microsoft.Identity.Client;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>Parses a Teams meeting join URL into Graph ChatInfo/MeetingInfo (as in Microsoft's samples).</summary>
public static partial class JoinInfo
{
    [GeneratedRegex("https://teams\\.microsoft\\.com.*/(?<thread>[^/]+)/(?<message>[^/]+)\\?context=(?<context>{.*})")]
    private static partial Regex MeetupJoin();

    public static (ChatInfo, MeetingInfo) Parse(string joinUrl)
    {
        var decoded = HttpUtility.UrlDecode(joinUrl);
        var match = MeetupJoin().Match(decoded);
        if (!match.Success)
            throw new ArgumentException("Unsupported Teams meeting link format", nameof(joinUrl));
        var context = System.Text.Json.JsonDocument.Parse(match.Groups["context"].Value).RootElement;
        var tenantId = context.GetProperty("Tid").GetString();
        var organizerId = context.GetProperty("Oid").GetString();
        var chatInfo = new ChatInfo
        {
            ThreadId = match.Groups["thread"].Value,
            MessageId = match.Groups["message"].Value,
            ReplyChainMessageId = context.TryGetProperty("MessageId", out var reply) ? reply.GetString() : null,
        };
        var meetingInfo = new OrganizerMeetingInfo
        {
            Organizer = new IdentitySet
            {
                User = new Identity
                {
                    Id = organizerId,
                    AdditionalData = new Dictionary<string, object> { ["tenantId"] = tenantId! },
                },
            },
        };
        return (chatInfo, meetingInfo);
    }
}

/// <summary>App-only token provider for Graph Communications + inbound request validation.</summary>
public sealed class AuthenticationProvider(string appId, string appSecret, ILogger logger) : IRequestAuthenticationProvider
{
    private readonly IConfidentialClientApplication app = ConfidentialClientApplicationBuilder
        .Create(appId).WithClientSecret(appSecret).WithAuthority("https://login.microsoftonline.com/common").Build();

    public async Task AuthenticateOutboundRequestAsync(HttpRequestMessage request, string tenant)
    {
        var result = await app.AcquireTokenForClient(["https://graph.microsoft.com/.default"])
            .WithTenantId(string.IsNullOrEmpty(tenant) ? "common" : tenant)
            .ExecuteAsync().ConfigureAwait(false);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", result.AccessToken);
    }

    // Microsoft signs call-signalling notifications with keys published here (as used by the
    // microsoft-graph-comms-samples). NOT VERIFIED against a live tenant.
    private const string OpenIdConfiguration = "https://api.aps.skype.com/v1/.well-known/OpenIdConfiguration";
    private static readonly string[] ValidIssuers = ["https://graph.microsoft.com", "https://api.botframework.com"];
    private readonly Microsoft.IdentityModel.Protocols.ConfigurationManager<Microsoft.IdentityModel.Protocols.OpenIdConnect.OpenIdConnectConfiguration> openId =
        new(OpenIdConfiguration, new Microsoft.IdentityModel.Protocols.OpenIdConnect.OpenIdConnectConfigurationRetriever());

    public async Task<RequestValidationResult> ValidateInboundRequestAsync(HttpRequestMessage request)
    {
        var token = request.Headers.Authorization?.Parameter;
        if (request.Headers.Authorization?.Scheme != "Bearer" || string.IsNullOrEmpty(token))
            return new RequestValidationResult { IsValid = false };
        try
        {
            var config = await openId.GetConfigurationAsync(CancellationToken.None).ConfigureAwait(false);
            var parameters = new Microsoft.IdentityModel.Tokens.TokenValidationParameters
            {
                ValidIssuers = ValidIssuers,
                ValidAudience = appId,
                IssuerSigningKeys = config.SigningKeys,
                ValidateLifetime = true,
                ClockSkew = TimeSpan.FromMinutes(5),
            };
            var handler = new System.IdentityModel.Tokens.Jwt.JwtSecurityTokenHandler();
            var principal = handler.ValidateToken(token, parameters, out _);
            var tenant = principal.FindFirst("http://schemas.microsoft.com/identity/claims/tenantid")?.Value;
            request.Properties.Add(HttpConstants.HeaderNames.Tenant, tenant);
            return new RequestValidationResult { IsValid = true, TenantId = tenant };
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "inbound_token_invalid");
            return new RequestValidationResult { IsValid = false };
        }
    }
}
