using System.Collections.Concurrent;
using System.Net;
using CallCopilot.TeamsMediaGateway.Core;
using Microsoft.Extensions.Logging.Abstractions;

namespace TeamsMediaGateway.Tests;

internal static class TestData
{
    public const string Secret = "test-shared-secret-0123456789abcdefghij";

    public static JoinRequest Join(string persistence = "TRANSIENT", bool receiveOnly = true) => new(
        CallId: "11111111-1111-1111-1111-111111111111",
        TenantId: "tenant-1",
        JoinUrl: "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%22Tid%22%3a%22tenant-1%22%2c%22Oid%22%3a%22organizer-1%22%7d",
        MediaWsUrl: "wss://api.example.com/api/v1/telephony/media/teams/1111?token=x",
        EventsUrl: "https://api.example.com/api/v1/integrations/teams/gateway/events",
        Persistence: persistence,
        SalespersonAadId: "sales-aad",
        ReceiveOnly: receiveOnly);

    public static byte[] Pcm(int ms = 20, short amplitude = 1000)
    {
        // 16 kHz, 16-bit mono sine wave (synthetic audio, in memory only).
        var samples = 16 * ms;
        var bytes = new byte[samples * 2];
        for (var i = 0; i < samples; i++)
        {
            var v = (short)(amplitude * Math.Sin(2 * Math.PI * 440 * i / 16000.0));
            bytes[2 * i] = (byte)(v & 0xff);
            bytes[2 * i + 1] = (byte)((v >> 8) & 0xff);
        }
        return bytes;
    }
}

/// <summary>Records gateway events instead of calling the API.</summary>
internal sealed class RecordingEventSink : ICallEventSink
{
    public ConcurrentQueue<GatewayEvent> Events { get; } = new();
    public Task SendAsync(GatewayEvent evt, string eventsUrl, CancellationToken ct = default)
    {
        Events.Enqueue(evt);
        return Task.CompletedTask;
    }
}

/// <summary>Controllable media sink: can fail connects/sends and be slowed down.</summary>
internal sealed class FakeSink : IMediaSink
{
    public ConcurrentQueue<AudioFrame> Sent { get; } = new();
    public int Connects;
    public int Closes;
    public int FailConnects;
    public int FailSends;
    public TaskCompletionSource? Gate;

    public Task ConnectAsync(CancellationToken ct)
    {
        Interlocked.Increment(ref Connects);
        if (FailConnects > 0) { FailConnects--; throw new System.Net.WebSockets.WebSocketException("connect refused"); }
        return Task.CompletedTask;
    }

    public async Task SendAsync(AudioFrame frame, CancellationToken ct)
    {
        if (Gate is not null) await Gate.Task.WaitAsync(ct);
        if (FailSends > 0) { FailSends--; throw new System.Net.WebSockets.WebSocketException("socket closed"); }
        Sent.Enqueue(frame);
    }

    public Task CloseAsync(CancellationToken ct) { Interlocked.Increment(ref Closes); return Task.CompletedTask; }
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal sealed class FakeBot : IMediaBot
{
    public List<JoinRequest> Joined { get; } = [];
    public List<string> Left { get; } = [];
    public Exception? JoinError;
    public MediaBotStatus Status { get; set; } = new(true, []);
    public int ActiveCalls => Joined.Count - Left.Count;

    public Task<string> JoinAsync(JoinRequest request, CancellationToken ct)
    {
        if (JoinError is not null) throw JoinError;
        Joined.Add(request);
        return Task.FromResult("gw-" + Joined.Count);
    }

    public Task LeaveAsync(string gatewayCallId) { Left.Add(gatewayCallId); return Task.CompletedTask; }

    public Task<HttpResponseMessage> ProcessNotificationAsync(HttpRequestMessage request) =>
        Task.FromResult(new HttpResponseMessage(HttpStatusCode.Accepted));
}

internal sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    public List<(HttpRequestMessage Request, byte[] Body)> Requests { get; } = [];
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var body = request.Content is null ? [] : await request.Content.ReadAsByteArrayAsync(ct);
        Requests.Add((request, body));
        return respond(request);
    }
}

internal static class Log
{
    public static readonly NullLogger Null = NullLogger.Instance;
}
