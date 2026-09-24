using System.Text.Json.Serialization;

namespace CallCopilot.TeamsMediaGateway.Core;

/// <summary>Join request from the CallCopilot API (POST /v1/calls, HMAC-signed).</summary>
public sealed record JoinRequest(
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("tenant_id")] string TenantId,
    [property: JsonPropertyName("join_url")] string JoinUrl,
    [property: JsonPropertyName("media_ws_url")] string MediaWsUrl,
    [property: JsonPropertyName("events_url")] string EventsUrl,
    [property: JsonPropertyName("persistence")] string Persistence,
    [property: JsonPropertyName("salesperson_aad_id")] string? SalespersonAadId,
    [property: JsonPropertyName("receive_only")] bool ReceiveOnly)
{
    public const string RecordingDeclared = "RECORDING_DECLARED";

    public IEnumerable<string> Problems()
    {
        if (string.IsNullOrWhiteSpace(CallId)) yield return "call_id";
        if (!Uri.TryCreate(MediaWsUrl, UriKind.Absolute, out var ws) || (ws.Scheme != "wss" && ws.Scheme != "ws")) yield return "media_ws_url";
        if (!Uri.TryCreate(EventsUrl, UriKind.Absolute, out var ev) || (ev.Scheme != "https" && ev.Scheme != "http")) yield return "events_url";
        if (!ReceiveOnly) yield return "receive_only must be true (the copilot never speaks)";
    }
}

/// <summary>Event sent to the API (POST events_url).</summary>
public sealed record GatewayEvent(
    [property: JsonPropertyName("event_id")] string EventId,
    [property: JsonPropertyName("call_id")] string CallId,
    [property: JsonPropertyName("gateway_call_id")] string GatewayCallId,
    [property: JsonPropertyName("state")] string? State,
    [property: JsonPropertyName("recording_status")] string? RecordingStatus = null,
    [property: JsonPropertyName("media_status")] string? MediaStatus = null,
    [property: JsonPropertyName("error_code")] string? ErrorCode = null);

public static class CallStates
{
    public const string Establishing = "ESTABLISHING";
    public const string Established = "ESTABLISHED";
    public const string Terminated = "TERMINATED";
    public const string Failed = "FAILED";
}

public interface ICallEventSink
{
    /// <summary>Delivers an event to the API. Must not throw (failures are logged).</summary>
    Task SendAsync(GatewayEvent evt, string eventsUrl, CancellationToken ct = default);
}

public sealed record MediaBotStatus(bool Ready, IReadOnlyList<string> Problems);

/// <summary>Everything the HTTP layer needs from the Teams bot (fakeable in tests).</summary>
public interface IMediaBot
{
    MediaBotStatus Status { get; }
    int ActiveCalls { get; }
    Task<string> JoinAsync(JoinRequest request, CancellationToken ct);
    Task LeaveAsync(string gatewayCallId);
    Task<HttpResponseMessage> ProcessNotificationAsync(HttpRequestMessage request);
}

public sealed class MediaBotUnavailableException(string message) : Exception(message);

public sealed class GatewayAtCapacityException() : Exception("gateway at capacity");

/// <summary>Used when the media platform cannot start (missing config, not Windows, init failure).
/// The gateway keeps serving /health with the reasons instead of failing silently.</summary>
public sealed class UnavailableMediaBot(IReadOnlyList<string> problems) : IMediaBot
{
    public MediaBotStatus Status { get; } = new(false, problems);
    public int ActiveCalls => 0;
    public Task<string> JoinAsync(JoinRequest request, CancellationToken ct) =>
        throw new MediaBotUnavailableException("Teams media platform is not available: " + string.Join("; ", problems));
    public Task LeaveAsync(string gatewayCallId) => Task.CompletedTask;
    public Task<HttpResponseMessage> ProcessNotificationAsync(HttpRequestMessage request) =>
        Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.ServiceUnavailable));
}
