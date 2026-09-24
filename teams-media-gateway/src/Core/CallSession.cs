namespace CallCopilot.TeamsMediaGateway.Core;

public enum CallSessionState
{
    Joining,
    Established,
    Forwarding,
    /// <summary>Recording could not be declared: compliance forbids forwarding audio.</summary>
    Blocked,
    /// <summary>Established but the API media socket could not be opened.</summary>
    MediaUnavailable,
    Ended,
}

/// <summary>
/// SDK-independent lifecycle + compliance rules of one joined Teams meeting.
///
/// COMPLIANCE (Microsoft policy for application-hosted media): nothing derived from call media
/// may be persisted unless the bot first set the call's recording status and received success.
/// - RECORDING_DECLARED: declare recording, report RECORDING_CONFIRMED, and only then forward
///   audio. If the declaration fails: report RECORDING_FAILED and forward NOTHING.
/// - TRANSIENT: forward audio for in-memory processing; the API stores nothing derived from it.
/// Audio is never written to disk here.
/// </summary>
public sealed class CallSession
{
    private readonly JoinRequest request;
    private readonly ICallEventSink events;
    private readonly Func<CancellationToken, Task> declareRecording;
    private readonly Func<AudioPipeline> pipelineFactory;
    private readonly ILogger logger;
    private readonly SemaphoreSlim gate = new(1, 1);
    private AudioPipeline? pipeline;
    private DateTimeOffset startedAt;

    public CallSession(JoinRequest request, string gatewayCallId, ICallEventSink events,
        Func<CancellationToken, Task> declareRecording, Func<AudioPipeline> pipelineFactory, ILogger logger)
    {
        this.request = request;
        GatewayCallId = gatewayCallId;
        this.events = events;
        this.declareRecording = declareRecording;
        this.pipelineFactory = pipelineFactory;
        this.logger = logger;
    }

    public string GatewayCallId { get; }
    public CallSessionState State { get; private set; } = CallSessionState.Joining;
    public PipelineStats? Stats => pipeline?.Stats;

    private GatewayEvent Event(string? state = null, string? recording = null, string? media = null, string? error = null) =>
        new(Guid.NewGuid().ToString("N"), request.CallId, GatewayCallId, state, recording, media, error);

    public Task OnJoiningAsync(CancellationToken ct = default) =>
        events.SendAsync(Event(CallStates.Establishing), request.EventsUrl, ct);

    public async Task OnEstablishedAsync(CancellationToken ct = default)
    {
        await gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (State != CallSessionState.Joining) return;
            State = CallSessionState.Established;
            startedAt = DateTimeOffset.UtcNow;
            logger.LogInformation("teams_media_session_started call_id={CallId} persistence={Persistence}", request.CallId, request.Persistence);
            await events.SendAsync(Event(CallStates.Established), request.EventsUrl, ct).ConfigureAwait(false);
            if (request.Persistence == JoinRequest.RecordingDeclared)
            {
                try
                {
                    // All participants see the Teams recording indicator from here on.
                    await declareRecording(ct).ConfigureAwait(false);
                    await events.SendAsync(Event(recording: "RECORDING_CONFIRMED"), request.EventsUrl, ct).ConfigureAwait(false);
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    logger.LogWarning("recording_status_failed call_id={CallId} error={Error}", request.CallId, ex.GetType().Name);
                    State = CallSessionState.Blocked;
                    await events.SendAsync(Event(recording: "RECORDING_FAILED", media: "UNAVAILABLE", error: "recording_status_failed"), request.EventsUrl, ct).ConfigureAwait(false);
                    return;
                }
            }
            var p = pipelineFactory();
            try
            {
                await p.StartAsync(ct).ConfigureAwait(false);
                pipeline = p;
                State = CallSessionState.Forwarding;
                await events.SendAsync(Event(media: "AVAILABLE"), request.EventsUrl, ct).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                // The meeting continues; only live assistance is unavailable.
                logger.LogWarning("media_socket_connect_failed call_id={CallId} error={Error}", request.CallId, ex.GetType().Name);
                State = CallSessionState.MediaUnavailable;
                await p.DisposeAsync().ConfigureAwait(false);
                await events.SendAsync(Event(media: "UNAVAILABLE", error: "media_socket_connect_failed"), request.EventsUrl, ct).ConfigureAwait(false);
            }
        }
        finally
        {
            gate.Release();
        }
    }

    /// <summary>Media thread entry point. Returns false when the frame was not accepted.</summary>
    public bool OnAudio(AudioFrame frame) =>
        State == CallSessionState.Forwarding && pipeline is not null && pipeline.TryWrite(frame);

    public async Task OnEndedAsync(bool failed, string? errorCode = null)
    {
        await gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (State == CallSessionState.Ended) return;
            State = CallSessionState.Ended;
            if (pipeline is not null)
            {
                await pipeline.StopAsync().ConfigureAwait(false);
                var s = pipeline.Stats;
                logger.LogInformation(
                    "teams_media_session_ended call_id={CallId} seconds={Seconds} frames_received={Received} frames_forwarded={Forwarded} frames_dropped={Dropped} frames_malformed={Malformed} reconnects={Reconnects}",
                    request.CallId, (int)(DateTimeOffset.UtcNow - startedAt).TotalSeconds, s.Received, s.Forwarded, s.Dropped, s.Malformed, s.Reconnects);
                await pipeline.DisposeAsync().ConfigureAwait(false);
            }
            await events.SendAsync(Event(failed ? CallStates.Failed : CallStates.Terminated, error: errorCode), request.EventsUrl).ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }
}
