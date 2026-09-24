using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Graph.Models;
using Microsoft.Skype.Bots.Media;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>
/// One joined meeting. Responsibilities:
/// - report lifecycle to the API (ESTABLISHING / ESTABLISHED / TERMINATED / FAILED);
/// - COMPLIANCE: when the company chose RECORDING_DECLARED, call updateRecordingStatus(Recording)
///   and wait for success BEFORE forwarding any audio (Microsoft policy for persisting media or
///   data derived from it). If that fails, report RECORDING_FAILED and forward nothing.
///   In TRANSIENT mode audio is forwarded for in-memory processing only (the API stores nothing
///   derived from it);
/// - forward unmixed 16 kHz PCM audio, labelled "agent" (the salesperson's Entra id) or
///   "customer", to the API media WebSocket. Audio is never written to disk.
/// </summary>
public sealed class CallHandler
{
    private readonly ICall call;
    private readonly ILocalMediaSession media;
    private readonly JoinRequest request;
    private readonly BackendClient backend;
    private readonly ILogger<CallHandler> logger;
    private readonly Action onEnded;
    private readonly CancellationTokenSource cts = new();
    private MediaForwarder? forwarder;
    private volatile bool forwarding;
    private int ended;

    public CallHandler(ICall call, ILocalMediaSession media, JoinRequest request, BackendClient backend,
        ILogger<CallHandler> logger, Action onEnded)
    {
        this.call = call;
        this.media = media;
        this.request = request;
        this.backend = backend;
        this.logger = logger;
        this.onEnded = onEnded;
        call.OnUpdated += OnCallUpdated;
        media.AudioSocket.AudioMediaReceived += OnAudio;
        _ = backend.SendEventAsync(request, call.Id, "ESTABLISHING");
    }

    private void OnCallUpdated(ICall sender, ResourceEventArgs<Call> args)
    {
        var state = args.NewResource.State;
        if (state == CallState.Established && args.OldResource.State != CallState.Established)
            _ = Task.Run(OnEstablishedAsync);
        else if (state == CallState.Terminated)
            _ = Task.Run(() => EndAsync(args.NewResource.ResultInfo?.Code is >= 400 ? "FAILED" : "TERMINATED"));
    }

    private async Task OnEstablishedAsync()
    {
        await backend.SendEventAsync(request, call.Id, "ESTABLISHED").ConfigureAwait(false);
        if (request.Persistence == "RECORDING_DECLARED")
        {
            try
            {
                // Teams shows the recording indicator to every participant from here on.
                await call.UpdateRecordingStatusAsync(RecordingStatus.Recording).ConfigureAwait(false);
                await backend.SendEventAsync(request, call.Id, null, recordingStatus: "RECORDING_CONFIRMED").ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                logger.LogWarning(ex, "recording_status_failed");
                await backend.SendEventAsync(request, call.Id, null, recordingStatus: "RECORDING_FAILED").ConfigureAwait(false);
                return; // compliance: no audio leaves the gateway
            }
        }
        forwarder = new MediaForwarder(new Uri(request.MediaWsUrl), logger);
        try
        {
            await forwarder.StartAsync(cts.Token).ConfigureAwait(false);
            forwarding = true;
        }
        catch (Exception ex)
        {
            // The meeting continues; only live assistance is unavailable.
            logger.LogWarning(ex, "media_socket_connect_failed");
        }
    }

    private void OnAudio(object? sender, AudioMediaReceivedEventArgs e)
    {
        try
        {
            if (!forwarding || forwarder is null) return;
            if (e.Buffer.UnmixedAudioBuffers is { Length: > 0 } unmixed)
            {
                foreach (var b in unmixed)
                {
                    var bytes = new byte[b.Length];
                    System.Runtime.InteropServices.Marshal.Copy(b.Data, bytes, 0, (int)b.Length);
                    _ = forwarder.SendAudioAsync(TrackFor(b.ActiveSpeakerId), bytes, cts.Token);
                }
            }
            else
            {
                var bytes = new byte[e.Buffer.Length];
                System.Runtime.InteropServices.Marshal.Copy(e.Buffer.Data, bytes, 0, (int)e.Buffer.Length);
                _ = forwarder.SendAudioAsync("mixed", bytes, cts.Token);
            }
        }
        finally
        {
            e.Buffer.Dispose(); // required by the media SDK; the audio is gone after this
        }
    }

    private string TrackFor(uint mediaSourceId)
    {
        if (string.IsNullOrEmpty(request.SalespersonAadId)) return "mixed";
        foreach (var p in call.Participants)
        {
            var sources = p.Resource.MediaStreams?.Where(s => s.MediaType == Modality.Audio) ?? [];
            if (sources.Any(s => uint.TryParse(s.SourceId, out var id) && id == mediaSourceId))
                return p.Resource.Info?.Identity?.User?.Id == request.SalespersonAadId ? "agent" : "customer";
        }
        return "customer";
    }

    public async Task LeaveAsync()
    {
        try
        {
            await call.DeleteAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            logger.LogWarning(ex, "leave_failed");
        }
        await EndAsync("TERMINATED").ConfigureAwait(false);
    }

    private async Task EndAsync(string state)
    {
        if (Interlocked.Exchange(ref ended, 1) == 1) return;
        forwarding = false;
        if (forwarder is not null)
        {
            try { await forwarder.StopAsync(CancellationToken.None).ConfigureAwait(false); }
            catch (Exception ex) { logger.LogDebug(ex, "stop_failed"); }
            await forwarder.DisposeAsync().ConfigureAwait(false);
        }
        await backend.SendEventAsync(request, call.Id, state).ConfigureAwait(false);
        media.AudioSocket.AudioMediaReceived -= OnAudio;
        cts.Cancel();
        onEnded();
    }
}
