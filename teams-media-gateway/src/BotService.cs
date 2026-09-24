using System.Collections.Concurrent;
using System.Net;
using System.Text.Json.Serialization;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>Join request from the CallCopilot API (POST /v1/calls, HMAC-signed).</summary>
public sealed record JoinRequest(
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("tenant_id")] string TenantId,
    [property: JsonPropertyName("join_url")] string JoinUrl,
    [property: JsonPropertyName("media_ws_url")] string MediaWsUrl,
    [property: JsonPropertyName("events_url")] string EventsUrl,
    [property: JsonPropertyName("persistence")] string Persistence,
    [property: JsonPropertyName("salesperson_aad_id")] string? SalespersonAadId,
    [property: JsonPropertyName("receive_only")] bool ReceiveOnly);

/// <summary>
/// Owns the Graph Communications client (application-hosted media). One instance per VM.
/// Pattern follows Microsoft's microsoft-graph-comms-samples (LocalMediaSamples). NOT BUILT.
/// </summary>
public sealed class BotService : IAsyncDisposable
{
    private readonly GatewayOptions options;
    private readonly BackendClient backend;
    private readonly ILoggerFactory loggerFactory;
    private readonly ILogger<BotService> logger;
    private readonly ConcurrentDictionary<string, CallHandler> handlers = new();
    private ICommunicationsClient? client;

    public BotService(GatewayOptions options, BackendClient backend, ILoggerFactory loggerFactory)
    {
        this.options = options;
        this.backend = backend;
        this.loggerFactory = loggerFactory;
        logger = loggerFactory.CreateLogger<BotService>();
    }

    public ICommunicationsClient Client => client ?? throw new InvalidOperationException("not initialised");

    public void Initialize()
    {
        var graphLogger = new GraphLogger(typeof(BotService).Assembly.GetName().Name);
        var builder = new CommunicationsClientBuilder("CallCopilotTeamsMediaGateway", options.AppId, graphLogger);
        var auth = new AuthenticationProvider(options.AppId, options.AppSecret, loggerFactory.CreateLogger("auth"));
        builder.SetAuthenticationProvider(auth);
        builder.SetNotificationUrl(new Uri($"{options.CallbackBaseUrl.TrimEnd('/')}/api/calling"));
        builder.SetMediaPlatformSettings(new MediaPlatformSettings
        {
            ApplicationId = options.AppId,
            MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
            {
                CertificateThumbprint = options.CertificateThumbprint,
                InstanceInternalPort = options.InstanceInternalPort,
                InstancePublicIPAddress = IPAddress.Parse(options.InstancePublicIPAddress),
                InstancePublicPort = options.InstancePublicPort,
                ServiceFqdn = options.ServiceDnsName,
            },
        });
        builder.SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"));
        client = builder.Build();
        client.Calls().OnIncoming += (_, args) =>
        {
            // The copilot only joins meetings it was asked to join; ignore unsolicited calls.
            foreach (var call in args.AddedResources)
                _ = call.DeleteAsync();
        };
    }

    public int ActiveCalls => handlers.Count;

    public async Task<string> JoinAsync(JoinRequest request, CancellationToken ct)
    {
        if (handlers.Count >= options.MaxConcurrentCalls)
            throw new InvalidOperationException("gateway at capacity");

        var (chatInfo, meetingInfo) = JoinInfo.Parse(request.JoinUrl);
        var mediaSession = Client.CreateMediaSession(
            new AudioSocketSettings
            {
                // Listen-only: the AI never speaks in the meeting.
                StreamDirections = StreamDirection.Recvonly,
                SupportedAudioFormat = AudioFormat.Pcm16K,
                // Per-speaker (unmixed) audio lets us separate salesperson and customer.
                ReceiveUnmixedMeetingAudio = true,
            },
            videoSocketSettings: null,
            vbssSocketSettings: null,
            mediaSessionId: Guid.NewGuid());

        var joinParams = new JoinMeetingParameters(chatInfo, meetingInfo, mediaSession)
        {
            TenantId = request.TenantId,
        };
        var call = await Client.Calls().AddAsync(joinParams, scenarioId: Guid.NewGuid()).ConfigureAwait(false);
        var handler = new CallHandler(call, mediaSession, request, backend,
            loggerFactory.CreateLogger<CallHandler>(), () => handlers.TryRemove(call.Id, out _));
        handlers[call.Id] = handler;
        logger.LogInformation("joined {GatewayCallId} for {CallId}", call.Id, request.CallId);
        return call.Id;
    }

    public async Task LeaveAsync(string gatewayCallId)
    {
        if (handlers.TryGetValue(gatewayCallId, out var handler))
            await handler.LeaveAsync().ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        foreach (var h in handlers.Values) await h.LeaveAsync().ConfigureAwait(false);
        if (client is not null) await client.TerminateAsync().ConfigureAwait(false);
    }
}
