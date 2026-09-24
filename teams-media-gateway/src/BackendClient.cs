using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>Signed call events to the CallCopilot API (POST /api/v1/integrations/teams/gateway/events).</summary>
public sealed class BackendClient(HttpClient http, GatewayOptions options, ILogger<BackendClient> logger)
{
    public async Task SendEventAsync(JoinRequest call, string gatewayCallId, string? state,
        string? recordingStatus = null, string? errorCode = null, CancellationToken ct = default)
    {
        var body = JsonSerializer.SerializeToUtf8Bytes(new
        {
            event_id = Guid.NewGuid().ToString("N"),
            call_id = call.CallId,
            gateway_call_id = gatewayCallId,
            state,
            recording_status = recordingStatus,
            error_code = errorCode,
        });
        var (ts, sig) = Signing.Sign(options.BackendSharedSecret, body);
        using var req = new HttpRequestMessage(HttpMethod.Post, call.EventsUrl)
        {
            Content = new ByteArrayContent(body),
        };
        req.Content.Headers.ContentType = new("application/json");
        req.Headers.Add("X-CC-Timestamp", ts);
        req.Headers.Add("X-CC-Signature", sig);
        for (var attempt = 1; attempt <= 3; attempt++)
        {
            try
            {
                using var resp = await http.SendAsync(req.Clone(), ct);
                if (resp.IsSuccessStatusCode) return;
                logger.LogWarning("backend_event_rejected {Status}", (int)resp.StatusCode);
                if ((int)resp.StatusCode < 500) return; // not retryable
            }
            catch (HttpRequestException)
            {
                logger.LogWarning("backend_event_failed attempt {Attempt}", attempt);
            }
            await Task.Delay(TimeSpan.FromSeconds(attempt), ct);
        }
    }
}

/// <summary>
/// Forwards audio to the CallCopilot media WebSocket (the same JSON format the API's mock
/// provider uses). Audio is held only in memory; nothing is written to disk here.
/// A broken socket never ends the Teams call: frames are dropped until it reconnects.
/// </summary>
public sealed class MediaForwarder(Uri url, ILogger logger) : IAsyncDisposable
{
    private ClientWebSocket? socket;
    private long seq;
    private readonly SemaphoreSlim sendLock = new(1, 1);

    public async Task StartAsync(CancellationToken ct)
    {
        socket = new ClientWebSocket();
        await socket.ConnectAsync(url, ct);
        await SendJsonAsync(new { @event = "start", format = new { encoding = "linear16", sample_rate = 16000 } }, ct);
    }

    /// <param name="track">"agent" (salesperson), "customer" or "mixed".</param>
    public Task SendAudioAsync(string track, ReadOnlyMemory<byte> pcm16k, CancellationToken ct) =>
        SendJsonAsync(new
        {
            @event = "media",
            track,
            seq = Interlocked.Increment(ref seq),
            payload = Convert.ToBase64String(pcm16k.Span),
        }, ct);

    public async Task StopAsync(CancellationToken ct)
    {
        await SendJsonAsync(new { @event = "stop" }, ct);
        if (socket is { State: WebSocketState.Open })
            await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "call ended", ct);
    }

    private async Task SendJsonAsync(object message, CancellationToken ct)
    {
        if (socket is not { State: WebSocketState.Open }) return;
        var bytes = JsonSerializer.SerializeToUtf8Bytes(message);
        await sendLock.WaitAsync(ct);
        try
        {
            await socket.SendAsync(bytes, WebSocketMessageType.Text, true, ct);
        }
        catch (WebSocketException)
        {
            logger.LogWarning("media_forward_failed"); // call continues; transcription pauses
        }
        finally
        {
            sendLock.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        socket?.Dispose();
        sendLock.Dispose();
        await Task.CompletedTask;
    }
}

internal static class HttpRequestMessageExtensions
{
    public static HttpRequestMessage Clone(this HttpRequestMessage req)
    {
        var clone = new HttpRequestMessage(req.Method, req.RequestUri);
        if (req.Content is not null)
        {
            var bytes = req.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult();
            clone.Content = new ByteArrayContent(bytes);
            foreach (var h in req.Content.Headers) clone.Content.Headers.TryAddWithoutValidation(h.Key, h.Value);
        }
        foreach (var h in req.Headers) clone.Headers.TryAddWithoutValidation(h.Key, h.Value);
        return clone;
    }
}
