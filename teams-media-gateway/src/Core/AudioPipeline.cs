using System.Net.WebSockets;
using System.Text.Json;
using System.Threading.Channels;

namespace CallCopilot.TeamsMediaGateway.Core;

/// <summary>One audio frame in memory: 16 kHz, 16-bit little-endian mono PCM (Teams Pcm16K).</summary>
public sealed record AudioFrame(string Track, byte[] Pcm);

/// <summary>Destination of audio (the CallCopilot API media WebSocket in production).</summary>
public interface IMediaSink : IAsyncDisposable
{
    Task ConnectAsync(CancellationToken ct);
    Task SendAsync(AudioFrame frame, CancellationToken ct);
    Task CloseAsync(CancellationToken ct);
}

public sealed record PipelineStats(long Received, long Forwarded, long Dropped, long Malformed, int Reconnects, bool Connected, bool Failed);

/// <summary>
/// Bounded, single-consumer audio pipeline between the Teams media thread and the API socket.
///
/// - Backpressure: at most <c>capacity</c> frames are queued; when the socket is slow or
///   reconnecting the OLDEST frames are dropped and counted (live assistance prefers recent audio;
///   memory stays bounded; the Teams media thread is never blocked).
/// - Malformed frames (empty, odd length, oversized) are rejected and counted.
/// - Socket failures trigger reconnects with linear backoff; the Teams call is never affected.
/// - Audio is only held in memory and is gone once sent or dropped.
/// </summary>
public sealed class AudioPipeline : IAsyncDisposable
{
    public const int MaxFrameBytes = 64 * 1024;
    private readonly Channel<AudioFrame> channel;
    private readonly IMediaSink sink;
    private readonly ILogger logger;
    private readonly TimeSpan backoff;
    private readonly int maxReconnects;
    private readonly CancellationTokenSource cts = new();
    private Task? consumer;
    private long received, forwarded, dropped, malformed;
    private int reconnects;            // total reconnect attempts (stats)
    private int consecutiveFailures;   // reset after every successful send
    private volatile bool connected;
    private volatile bool failed;      // gave up: frames are dropped without further attempts

    public AudioPipeline(IMediaSink sink, int capacity, ILogger logger, TimeSpan? reconnectBackoff = null, int maxReconnects = 10)
    {
        this.sink = sink;
        this.logger = logger;
        backoff = reconnectBackoff ?? TimeSpan.FromSeconds(1);
        this.maxReconnects = maxReconnects;
        channel = Channel.CreateBounded<AudioFrame>(
            new BoundedChannelOptions(capacity) { FullMode = BoundedChannelFullMode.DropOldest, SingleReader = true },
            _ => Interlocked.Increment(ref dropped));
    }

    public PipelineStats Stats => new(
        Interlocked.Read(ref received), Interlocked.Read(ref forwarded), Interlocked.Read(ref dropped),
        Interlocked.Read(ref malformed), reconnects, connected, failed);

    /// <summary>Connects the sink and starts forwarding. Throws if the first connect fails.</summary>
    public async Task StartAsync(CancellationToken ct)
    {
        await sink.ConnectAsync(ct).ConfigureAwait(false);
        connected = true;
        consumer = Task.Run(() => ConsumeAsync(cts.Token));
    }

    /// <summary>Called from the media thread: never blocks, never throws.</summary>
    public bool TryWrite(AudioFrame frame)
    {
        if (frame.Pcm.Length == 0 || frame.Pcm.Length % 2 != 0 || frame.Pcm.Length > MaxFrameBytes)
        {
            Interlocked.Increment(ref malformed);
            return false;
        }
        Interlocked.Increment(ref received);
        return channel.Writer.TryWrite(frame);
    }

    private async Task ConsumeAsync(CancellationToken ct)
    {
        try
        {
            await foreach (var frame in channel.Reader.ReadAllAsync(ct).ConfigureAwait(false))
            {
                while (true)
                {
                    if (failed)
                    {
                        Interlocked.Increment(ref dropped); // keep draining so memory stays bounded
                        break;
                    }
                    try
                    {
                        if (!connected) await ReconnectAsync(ct).ConfigureAwait(false);
                        await sink.SendAsync(frame, ct).ConfigureAwait(false);
                        Interlocked.Increment(ref forwarded);
                        consecutiveFailures = 0;
                        break;
                    }
                    catch (OperationCanceledException) when (ct.IsCancellationRequested)
                    {
                        return;
                    }
                    catch (Exception ex) when (ex is WebSocketException or IOException or InvalidOperationException)
                    {
                        connected = false;
                        consecutiveFailures++;
                        logger.LogWarning("media_socket_send_failed consecutive_failures={Failures}", consecutiveFailures);
                        if (consecutiveFailures > maxReconnects)
                        {
                            failed = true;
                            logger.LogError("media_socket_gave_up reconnects={Reconnects}", reconnects);
                        }
                    }
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    private async Task ReconnectAsync(CancellationToken ct)
    {
        reconnects++;
        await Task.Delay(backoff * Math.Max(1, consecutiveFailures), ct).ConfigureAwait(false);
        await sink.ConnectAsync(ct).ConfigureAwait(false);
        connected = true;
        logger.LogInformation("media_socket_reconnected attempt={Attempt}", reconnects);
    }

    /// <summary>Stops forwarding: pending frames get a short grace period, then the sink closes.</summary>
    public async Task StopAsync(TimeSpan? drainTimeout = null)
    {
        channel.Writer.TryComplete();
        if (consumer is not null)
        {
            var finished = await Task.WhenAny(consumer, Task.Delay(drainTimeout ?? TimeSpan.FromSeconds(2))).ConfigureAwait(false);
            if (finished != consumer) cts.Cancel();
            try { await consumer.ConfigureAwait(false); } catch (OperationCanceledException) { }
        }
        try
        {
            if (connected) await sink.CloseAsync(CancellationToken.None).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is WebSocketException or IOException or InvalidOperationException)
        {
            logger.LogDebug("media_socket_close_failed");
        }
        connected = false;
    }

    public async ValueTask DisposeAsync()
    {
        cts.Cancel();
        await sink.DisposeAsync().ConfigureAwait(false);
        cts.Dispose();
    }
}

/// <summary>
/// The CallCopilot API media WebSocket, in the API's JSON media format:
/// {"event":"start","format":{"encoding":"linear16","sample_rate":16000}},
/// {"event":"media","track":"agent|customer|mixed","seq":n,"payload":"&lt;base64&gt;"}, {"event":"stop"}.
/// The URL carries a per-call HMAC token issued by the API and must not be logged.
/// </summary>
public sealed class WebSocketMediaSink(Uri url, Func<ClientWebSocket>? socketFactory = null) : IMediaSink
{
    private ClientWebSocket? socket;
    private long seq;

    public async Task ConnectAsync(CancellationToken ct)
    {
        socket?.Dispose();
        socket = (socketFactory ?? (() => new ClientWebSocket()))();
        await socket.ConnectAsync(url, ct).ConfigureAwait(false);
        await SendJsonAsync(new { @event = "start", format = new { encoding = "linear16", sample_rate = 16000 } }, ct).ConfigureAwait(false);
    }

    public Task SendAsync(AudioFrame frame, CancellationToken ct) =>
        SendJsonAsync(new { @event = "media", track = frame.Track, seq = Interlocked.Increment(ref seq), payload = Convert.ToBase64String(frame.Pcm) }, ct);

    public async Task CloseAsync(CancellationToken ct)
    {
        if (socket is not { State: WebSocketState.Open }) return;
        await SendJsonAsync(new { @event = "stop" }, ct).ConfigureAwait(false);
        await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "call ended", ct).ConfigureAwait(false);
    }

    private async Task SendJsonAsync(object message, CancellationToken ct)
    {
        if (socket is not { State: WebSocketState.Open })
            throw new InvalidOperationException("media socket not open");
        await socket.SendAsync(JsonSerializer.SerializeToUtf8Bytes(message), WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
    }

    public ValueTask DisposeAsync()
    {
        socket?.Dispose();
        return ValueTask.CompletedTask;
    }
}
