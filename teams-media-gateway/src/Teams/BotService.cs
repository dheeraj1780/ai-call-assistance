using System.Collections.Concurrent;
using System.Net;
using System.Runtime.InteropServices;
using CallCopilot.TeamsMediaGateway.Core;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;

namespace CallCopilot.TeamsMediaGateway.Teams;

/// <summary>
/// Owns the Graph Communications client with application-hosted media (one per VM). Follows the
/// pattern of Microsoft's microsoft-graph-comms-samples. Business logic stays in the API; this class
/// only joins meetings, receives audio and reports lifecycle.
/// </summary>
public sealed class BotService : IMediaBot, IAsyncDisposable
{
    private readonly GatewayOptions options;
    private readonly ICallEventSink events;
    private readonly ILoggerFactory loggerFactory;
    private readonly ILogger<BotService> logger;
    private readonly ConcurrentDictionary<string, CallHandler> handlers = new();
    private readonly ICommunicationsClient client;

    private BotService(GatewayOptions options, ICallEventSink events, ILoggerFactory loggerFactory, ICommunicationsClient client)
    {
        this.options = options;
        this.events = events;
        this.loggerFactory = loggerFactory;
        logger = loggerFactory.CreateLogger<BotService>();
        this.client = client;
        client.Calls().OnIncoming += (collection, args) =>
        {
            // The copilot joins only meetings the API asked for; unsolicited calls are rejected.
            foreach (var incoming in args.AddedResources) _ = incoming.DeleteAsync();
        };
    }

    /// <summary>Builds the client and starts the media platform. Throws when the platform cannot
    /// start (e.g. certificate not found, not Windows Server); the caller then runs degraded.</summary>
    public static BotService Create(GatewayOptions options, ICallEventSink events, ILoggerFactory loggerFactory)
    {
        var graphLogger = new GraphLogger("CallCopilotTeamsMediaGateway");
        var builder = new CommunicationsClientBuilder("CallCopilotTeamsMediaGateway", options.AppId, graphLogger);
        builder.SetAuthentication(options.AppId, new TokenProvider(options));
        builder.SetNotificationUrl(new Uri($"{options.CallbackBaseUrl.TrimEnd('/')}/api/calling"));
        builder.SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"));
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
        return new BotService(options, events, loggerFactory, builder.Build());
    }

    public MediaBotStatus Status => new(true, []);
    public int ActiveCalls => handlers.Count;

    public async Task<string> JoinAsync(JoinRequest request, CancellationToken ct)
    {
        if (handlers.Count >= options.MaxConcurrentCalls) throw new GatewayAtCapacityException();
        var (chat, meeting, linkTenant) = JoinInfo.Parse(request.JoinUrl);
        var mediaSession = client.CreateMediaSession(
            new AudioSocketSettings
            {
                // Listen-only: the AI never speaks in the meeting.
                StreamDirections = StreamDirection.Recvonly,
                SupportedAudioFormat = AudioFormat.Pcm16K,
                // Per-speaker (unmixed) audio separates salesperson and customer.
                ReceiveUnmixedMeetingAudio = true,
            },
            (VideoSocketSettings?)null,
            null,
            null,
            Guid.NewGuid());
        var joinParams = new JoinMeetingParameters(chat, meeting, mediaSession)
        {
            TenantId = string.IsNullOrEmpty(request.TenantId) ? linkTenant : request.TenantId,
        };
        var call = await client.Calls().AddAsync(joinParams, Guid.NewGuid(), ct).ConfigureAwait(false);
        var handler = new CallHandler(call, mediaSession, request, events, options, loggerFactory, () => handlers.TryRemove(call.Id, out _));
        handlers[call.Id] = handler;
        await handler.StartAsync(ct).ConfigureAwait(false);
        logger.LogInformation("teams_meeting_joined call_id={CallId}", request.CallId);
        return call.Id;
    }

    public async Task LeaveAsync(string gatewayCallId)
    {
        if (handlers.TryGetValue(gatewayCallId, out var handler)) await handler.LeaveAsync().ConfigureAwait(false);
    }

    public Task<HttpResponseMessage> ProcessNotificationAsync(HttpRequestMessage request) => client.ProcessNotificationAsync(request);

    public async ValueTask DisposeAsync()
    {
        foreach (var h in handlers.Values) await h.LeaveAsync().ConfigureAwait(false);
        await client.TerminateAsync().ConfigureAwait(false);
    }
}

/// <summary>Adapts one Graph <see cref="ICall"/> + media session to the SDK-free <see cref="CallSession"/>.</summary>
public sealed class CallHandler
{
    private readonly ICall call;
    private readonly ILocalMediaSession media;
    private readonly JoinRequest request;
    private readonly CallSession session;
    private readonly Action onEnded;
    private readonly ILogger logger;
    private readonly ConcurrentDictionary<uint, string> trackBySource = new();

    public CallHandler(ICall call, ILocalMediaSession media, JoinRequest request, ICallEventSink events,
        GatewayOptions options, ILoggerFactory loggerFactory, Action onEnded)
    {
        this.call = call;
        this.media = media;
        this.request = request;
        this.onEnded = onEnded;
        logger = loggerFactory.CreateLogger<CallHandler>();
        session = new CallSession(request, call.Id, events,
            ct => call.UpdateRecordingStatusAsync(RecordingStatus.Recording, ct),
            () => new AudioPipeline(new WebSocketMediaSink(new Uri(request.MediaWsUrl)), options.AudioQueueFrames, logger,
                TimeSpan.FromSeconds(options.MediaReconnectBackoffSeconds), options.MaxMediaReconnects),
            logger);
        call.OnUpdated += OnCallUpdated;
        call.Participants.OnUpdated += (_, _) => RefreshParticipants();
        media.AudioSocket.AudioMediaReceived += OnAudio;
    }

    public Task StartAsync(CancellationToken ct) => session.OnJoiningAsync(ct);

    private void OnCallUpdated(ICall sender, ResourceEventArgs<Call> args)
    {
        var state = args.NewResource.State;
        if (state == CallState.Established && args.OldResource?.State != CallState.Established)
            _ = Task.Run(() => session.OnEstablishedAsync());
        else if (state == CallState.Terminated)
        {
            var failed = args.NewResource.ResultInfo?.Code is >= 400;
            // Teams' own termination reason (code + subcode + message) - the code alone cannot be
            // attributed. Only these diagnostic fields are logged (no tokens, URLs or content).
            var (code, subcode, message) = CallDiagnostics.FromResultInfo(args.NewResource.ResultInfo);
            logger.Log(failed ? Microsoft.Extensions.Logging.LogLevel.Warning : Microsoft.Extensions.Logging.LogLevel.Information,
                "teams_call_terminated call_id={CallId} gateway_call_id={GatewayCallId} result_code={ResultCode} result_subcode={ResultSubcode} result_message={ResultMessage}",
                request.CallId, call.Id, code?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "none",
                subcode?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "none", message);
            _ = Task.Run(() => EndAsync(failed, failed ? $"teams_{args.NewResource.ResultInfo?.Code}" : null));
        }
    }

    private void RefreshParticipants()
    {
        foreach (var p in call.Participants)
        {
            var aad = p.Resource?.Info?.Identity?.User?.Id;
            foreach (var s in p.Resource?.MediaStreams ?? [])
            {
                if (s.MediaType == Modality.Audio && uint.TryParse(s.SourceId, out var id))
                    trackBySource[id] = !string.IsNullOrEmpty(request.SalespersonAadId) && aad == request.SalespersonAadId ? "agent" : "customer";
            }
        }
    }

    private string TrackFor(uint sourceId)
    {
        if (string.IsNullOrEmpty(request.SalespersonAadId)) return "mixed";
        if (!trackBySource.TryGetValue(sourceId, out var track))
        {
            RefreshParticipants();
            trackBySource.TryGetValue(sourceId, out track);
        }
        return track ?? "customer";
    }

    private void OnAudio(object? sender, AudioMediaReceivedEventArgs e)
    {
        try
        {
            if (session.State != CallSessionState.Forwarding) return; // compliance / not ready: drop
            if (e.Buffer.UnmixedAudioBuffers is { Length: > 0 } unmixed)
            {
                foreach (var b in unmixed)
                    session.OnAudio(new AudioFrame(TrackFor(b.ActiveSpeakerId), Copy(b.Data, b.Length)));
            }
            else if (!e.Buffer.IsSilence)
            {
                session.OnAudio(new AudioFrame("mixed", Copy(e.Buffer.Data, e.Buffer.Length)));
            }
        }
        finally
        {
            e.Buffer.Dispose(); // required by the media SDK; the audio is gone after this
        }
    }

    private static byte[] Copy(IntPtr data, long length)
    {
        if (length <= 0 || length > AudioPipeline.MaxFrameBytes) return [];
        var bytes = new byte[length];
        Marshal.Copy(data, bytes, 0, (int)length);
        return bytes;
    }

    public async Task LeaveAsync()
    {
        try
        {
            await call.DeleteAsync().ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Terminated notification or EndAsync below still reports the end.
        }
        await EndAsync(false, null).ConfigureAwait(false);
    }

    private async Task EndAsync(bool failed, string? error)
    {
        media.AudioSocket.AudioMediaReceived -= OnAudio;
        await session.OnEndedAsync(failed, error).ConfigureAwait(false);
        onEnded();
    }
}
