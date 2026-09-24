using System.Net;
using System.Text.Json;

namespace CallCopilot.TeamsMediaGateway.Core;

/// <summary>Signed call events to the CallCopilot API. Retries network errors and 5xx a few times;
/// never throws (a lost event must not break the Teams call).</summary>
public sealed class BackendClient(HttpClient http, GatewayOptions options, ILogger<BackendClient> logger) : ICallEventSink
{
    public int MaxAttempts { get; init; } = 3;
    public TimeSpan RetryDelay { get; init; } = TimeSpan.FromSeconds(1);

    public async Task SendAsync(GatewayEvent evt, string eventsUrl, CancellationToken ct = default)
    {
        var body = JsonSerializer.SerializeToUtf8Bytes(evt);
        for (var attempt = 1; attempt <= MaxAttempts; attempt++)
        {
            var (ts, sig) = Signing.Sign(options.BackendSharedSecret, body);
            using var req = new HttpRequestMessage(HttpMethod.Post, eventsUrl) { Content = new ByteArrayContent(body) };
            req.Content.Headers.ContentType = new("application/json");
            req.Headers.Add(Signing.TimestampHeader, ts);
            req.Headers.Add(Signing.SignatureHeader, sig);
            try
            {
                using var resp = await http.SendAsync(req, ct).ConfigureAwait(false);
                if (resp.IsSuccessStatusCode) return;
                logger.LogWarning("backend_event_rejected status={Status} attempt={Attempt}", (int)resp.StatusCode, attempt);
                if ((int)resp.StatusCode < 500 && resp.StatusCode != HttpStatusCode.TooManyRequests) return;
            }
            catch (HttpRequestException)
            {
                logger.LogWarning("backend_connection_failed attempt={Attempt}", attempt);
            }
            catch (TaskCanceledException) when (!ct.IsCancellationRequested)
            {
                logger.LogWarning("backend_event_timeout attempt={Attempt}", attempt);
            }
            if (attempt < MaxAttempts)
            {
                try { await Task.Delay(RetryDelay * attempt, ct).ConfigureAwait(false); }
                catch (OperationCanceledException) { return; }
            }
        }
        logger.LogError("backend_event_lost call_id={CallId}", evt.CallId);
    }
}
