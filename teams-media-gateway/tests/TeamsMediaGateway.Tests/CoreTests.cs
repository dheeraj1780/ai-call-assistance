using System.Net;
using System.Text;
using System.Text.Json;
using CallCopilot.TeamsMediaGateway.Core;
using CallCopilot.TeamsMediaGateway.Teams;
using Microsoft.Extensions.Logging.Abstractions;

namespace TeamsMediaGateway.Tests;

public class SigningTests
{
    [Fact]
    public void Matches_the_api_algorithm()
    {
        // Same vector the Python API computes: hex(HMAC-SHA256(secret, "1700000000." + body)).
        var (ts, sig) = Signing.Sign("s", Encoding.UTF8.GetBytes("{}"), 1700000000);
        Assert.Equal("1700000000", ts);
        var expected = Convert.ToHexString(System.Security.Cryptography.HMACSHA256.HashData(
            Encoding.UTF8.GetBytes("s"), Encoding.UTF8.GetBytes("1700000000.{}"))).ToLowerInvariant();
        Assert.Equal("v1=" + expected, sig);
    }

    [Fact]
    public void Verify_rejects_tampering_wrong_secret_and_replay()
    {
        var body = Encoding.UTF8.GetBytes("{\"a\":1}");
        var (ts, sig) = Signing.Sign(TestData.Secret, body);
        Assert.True(Signing.Verify(TestData.Secret, ts, sig, body));
        Assert.False(Signing.Verify(TestData.Secret, ts, sig, Encoding.UTF8.GetBytes("{\"a\":2}")));
        Assert.False(Signing.Verify("another-secret-0123456789abcdefghijkl", ts, sig, body));
        Assert.False(Signing.Verify(TestData.Secret, ts, null, body));
        Assert.False(Signing.Verify(TestData.Secret, "not-a-number", sig, body));
        var (oldTs, oldSig) = Signing.Sign(TestData.Secret, body, DateTimeOffset.UtcNow.AddMinutes(-6).ToUnixTimeSeconds());
        Assert.False(Signing.Verify(TestData.Secret, oldTs, oldSig, body));
    }
}

public class OptionsTests
{
    [Fact]
    public void Missing_shared_secret_is_fatal()
    {
        Assert.Contains(GatewayOptionsValidator.Fatal(new GatewayOptions()), p => p.Contains("BackendSharedSecret"));
        Assert.Empty(GatewayOptionsValidator.Fatal(new GatewayOptions { BackendSharedSecret = TestData.Secret }));
    }

    [Fact]
    public void Media_problems_are_listed_not_hidden()
    {
        var problems = GatewayOptionsValidator.Media(new GatewayOptions { InstancePublicIPAddress = "0.0.0.0", CallbackBaseUrl = "http://x" });
        Assert.Contains(problems, p => p.Contains("AppId"));
        Assert.Contains(problems, p => p.Contains("AppSecret"));
        Assert.Contains(problems, p => p.Contains("CertificateThumbprint"));
        Assert.Contains(problems, p => p.Contains("InstancePublicIPAddress"));
        Assert.Contains(problems, p => p.Contains("https"));
        var complete = new GatewayOptions
        {
            AppId = Guid.NewGuid().ToString(), AppSecret = "x", ServiceDnsName = "media.example.com", CertificateThumbprint = "ABC",
            InstancePublicIPAddress = "20.1.2.3", CallbackBaseUrl = "https://media.example.com",
        };
        Assert.Empty(GatewayOptionsValidator.Media(complete));
    }
}

public class JoinInfoTests
{
    [Fact]
    public void Parses_classic_meeting_link()
    {
        var (chat, meeting, tenant) = JoinInfo.Parse(TestData.Join().JoinUrl);
        Assert.Equal("19:meeting_abc@thread.v2", chat.ThreadId);
        Assert.Equal("0", chat.MessageId);
        Assert.Equal("tenant-1", tenant);
        Assert.Equal("organizer-1", meeting.Organizer?.User?.Id);
    }

    [Theory]
    [InlineData("https://teams.microsoft.com/meet/123456?p=abc")]
    [InlineData("https://evil.example.com/l/meetup-join/19%3ax/0?context=%7b%7d")]
    [InlineData("https://teams.microsoft.com/l/meetup-join/19%3ax/0?context=%7bnot-json")]
    [InlineData("https://teams.microsoft.com/l/meetup-join/19%3ax/0?context=%7b%22Tid%22%3a%22t%22%7d")]
    public void Rejects_unsupported_or_malformed_links(string url) =>
        Assert.Throws<ArgumentException>(() => JoinInfo.Parse(url));
}

public class JoinRequestTests
{
    [Fact]
    public void Receive_only_is_mandatory() =>
        Assert.Contains(TestData.Join(receiveOnly: false).Problems(), p => p.Contains("receive_only"));

    [Fact]
    public void Deserialises_api_payload()
    {
        var json = """{"call_id":"c","tenant_id":"t","join_url":"u","media_ws_url":"wss://a/b","events_url":"https://a/e","persistence":"TRANSIENT","salesperson_aad_id":null,"receive_only":true}""";
        var r = JsonSerializer.Deserialize<JoinRequest>(json)!;
        Assert.Equal("c", r.CallId);
        Assert.Empty(r.Problems());
    }
}

public class AudioPipelineTests
{
    private static async Task WaitUntil(Func<bool> condition)
    {
        for (var i = 0; i < 200 && !condition(); i++) await Task.Delay(10);
        Assert.True(condition());
    }

    [Fact]
    public async Task Forwards_frames_in_order()
    {
        var sink = new FakeSink();
        var p = new AudioPipeline(sink, 50, NullLogger.Instance);
        await p.StartAsync(CancellationToken.None);
        for (var i = 0; i < 5; i++) Assert.True(p.TryWrite(new AudioFrame(i % 2 == 0 ? "agent" : "customer", TestData.Pcm())));
        await WaitUntil(() => sink.Sent.Count == 5);
        Assert.Equal(["agent", "customer", "agent", "customer", "agent"], sink.Sent.Select(f => f.Track));
        await p.StopAsync();
        Assert.Equal(1, sink.Closes);
        Assert.Equal(5, p.Stats.Forwarded);
    }

    [Fact]
    public async Task Backpressure_drops_oldest_frames_and_counts_them()
    {
        var sink = new FakeSink { Gate = new TaskCompletionSource() };
        var p = new AudioPipeline(sink, 10, NullLogger.Instance);
        await p.StartAsync(CancellationToken.None);
        for (var i = 0; i < 100; i++) p.TryWrite(new AudioFrame("customer", TestData.Pcm()));
        Assert.True(p.Stats.Dropped >= 80, $"dropped={p.Stats.Dropped}");
        sink.Gate.SetResult();
        await p.StopAsync();
        Assert.Equal(100, p.Stats.Received);
        Assert.True(p.Stats.Forwarded <= 11);
        Assert.Equal(p.Stats.Received, p.Stats.Forwarded + p.Stats.Dropped);
    }

    [Fact]
    public async Task Malformed_frames_are_rejected()
    {
        var sink = new FakeSink();
        var p = new AudioPipeline(sink, 10, NullLogger.Instance);
        await p.StartAsync(CancellationToken.None);
        Assert.False(p.TryWrite(new AudioFrame("agent", [])));
        Assert.False(p.TryWrite(new AudioFrame("agent", new byte[3])));
        Assert.False(p.TryWrite(new AudioFrame("agent", new byte[AudioPipeline.MaxFrameBytes + 2])));
        await p.StopAsync();
        Assert.Equal(3, p.Stats.Malformed);
        Assert.Empty(sink.Sent);
    }

    [Fact]
    public async Task Reconnects_after_socket_failure()
    {
        var sink = new FakeSink { FailSends = 1 };
        var p = new AudioPipeline(sink, 50, NullLogger.Instance, TimeSpan.FromMilliseconds(5));
        await p.StartAsync(CancellationToken.None);
        p.TryWrite(new AudioFrame("agent", TestData.Pcm()));
        p.TryWrite(new AudioFrame("agent", TestData.Pcm()));
        await WaitUntil(() => sink.Sent.Count == 2);
        await p.StopAsync();
        Assert.Equal(1, p.Stats.Reconnects);
        Assert.Equal(2, sink.Connects);
    }

    [Fact]
    public async Task Gives_up_after_max_reconnects_without_blocking()
    {
        // Connected at first, then every send fails: bounded reconnects, frames dropped, no hang.
        var sink = new FakeSink { FailSends = int.MaxValue };
        var p = new AudioPipeline(sink, 20, NullLogger.Instance, TimeSpan.FromMilliseconds(1), maxReconnects: 2);
        await p.StartAsync(CancellationToken.None);
        for (var i = 0; i < 5; i++) Assert.True(p.TryWrite(new AudioFrame("agent", TestData.Pcm())));
        await WaitUntil(() => p.Stats.Dropped == 5);
        var stopped = p.StopAsync(TimeSpan.FromMilliseconds(500));
        Assert.Same(stopped, await Task.WhenAny(stopped, Task.Delay(TimeSpan.FromSeconds(5))));
        Assert.Equal(2, p.Stats.Reconnects);
        Assert.Empty(sink.Sent);
        Assert.Equal(0, p.Stats.Forwarded);
    }

    [Fact]
    public async Task First_connect_failure_is_reported()
    {
        var sink = new FakeSink { FailConnects = 1 };
        var p = new AudioPipeline(sink, 10, NullLogger.Instance);
        await Assert.ThrowsAsync<System.Net.WebSockets.WebSocketException>(() => p.StartAsync(CancellationToken.None));
    }
}

public class CallSessionTests
{
    private static (CallSession Session, RecordingEventSink Events, FakeSink Sink) Create(
        string persistence, Func<CancellationToken, Task>? declare = null, FakeSink? sink = null)
    {
        var events = new RecordingEventSink();
        sink ??= new FakeSink();
        var s = sink;
        var session = new CallSession(TestData.Join(persistence), "gw-1", events,
            declare ?? (_ => Task.CompletedTask), () => new AudioPipeline(s, 50, NullLogger.Instance), NullLogger.Instance);
        return (session, events, sink);
    }

    private static List<string?> Kinds(RecordingEventSink e) =>
        e.Events.Select(x => x.State ?? x.RecordingStatus ?? x.MediaStatus).ToList();

    [Fact]
    public async Task Transient_call_forwards_audio_and_reports_lifecycle()
    {
        var (session, events, sink) = Create("TRANSIENT");
        await session.OnJoiningAsync();
        Assert.False(session.OnAudio(new AudioFrame("customer", TestData.Pcm()))); // not established yet
        await session.OnEstablishedAsync();
        Assert.Equal(CallSessionState.Forwarding, session.State);
        Assert.True(session.OnAudio(new AudioFrame("customer", TestData.Pcm())));
        await session.OnEndedAsync(failed: false);
        Assert.Single(sink.Sent);
        Assert.Equal(["ESTABLISHING", "ESTABLISHED", "AVAILABLE", "TERMINATED"], Kinds(events));
        Assert.All(events.Events, e => Assert.Equal("gw-1", e.GatewayCallId));
    }

    [Fact]
    public async Task Declared_recording_is_confirmed_before_any_audio()
    {
        var declared = false;
        var (session, events, _) = Create(JoinRequest.RecordingDeclared, _ => { declared = true; return Task.CompletedTask; });
        await session.OnEstablishedAsync();
        Assert.True(declared);
        Assert.Equal(["ESTABLISHED", "RECORDING_CONFIRMED", "AVAILABLE"], Kinds(events));
        Assert.True(session.OnAudio(new AudioFrame("agent", TestData.Pcm())));
    }

    [Fact]
    public async Task Failed_recording_declaration_blocks_all_audio()
    {
        var (session, events, sink) = Create(JoinRequest.RecordingDeclared, _ => throw new InvalidOperationException("graph 403"));
        await session.OnEstablishedAsync();
        Assert.Equal(CallSessionState.Blocked, session.State);
        Assert.False(session.OnAudio(new AudioFrame("agent", TestData.Pcm())));
        Assert.Equal(0, sink.Connects); // the media socket is never even opened
        var failed = events.Events.Last();
        Assert.Equal("RECORDING_FAILED", failed.RecordingStatus);
        Assert.Equal("UNAVAILABLE", failed.MediaStatus);
    }

    [Fact]
    public async Task Backend_media_socket_failure_is_reported_and_call_continues()
    {
        var (session, events, _) = Create("TRANSIENT", sink: new FakeSink { FailConnects = 1 });
        await session.OnEstablishedAsync();
        Assert.Equal(CallSessionState.MediaUnavailable, session.State);
        Assert.Equal("media_socket_connect_failed", events.Events.Last().ErrorCode);
        await session.OnEndedAsync(failed: false);
        Assert.Equal("TERMINATED", events.Events.Last().State);
    }

    [Fact]
    public async Task Ending_twice_reports_once_and_failure_code_is_kept()
    {
        var (session, events, sink) = Create("TRANSIENT");
        await session.OnEstablishedAsync();
        await session.OnEndedAsync(failed: true, errorCode: "teams_500");
        await session.OnEndedAsync(failed: false);
        Assert.Single(events.Events, e => e.State is "FAILED" or "TERMINATED");
        Assert.Equal("teams_500", events.Events.Last().ErrorCode);
        Assert.Equal(1, sink.Closes);
    }
}

public class BackendClientTests
{
    private static BackendClient Client(StubHandler handler) =>
        new(new HttpClient(handler), new GatewayOptions { BackendSharedSecret = TestData.Secret }, NullLogger<BackendClient>.Instance)
        { RetryDelay = TimeSpan.FromMilliseconds(1) };

    private static GatewayEvent Evt() => new("e1", "c1", "gw-1", "ESTABLISHED");

    [Fact]
    public async Task Events_are_signed_for_the_api()
    {
        var handler = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.OK));
        await Client(handler).SendAsync(Evt(), "https://api.example.com/events");
        var (req, body) = Assert.Single(handler.Requests);
        Assert.True(Signing.Verify(TestData.Secret, req.Headers.GetValues(Signing.TimestampHeader).Single(), req.Headers.GetValues(Signing.SignatureHeader).Single(), body));
        using var doc = JsonDocument.Parse(body);
        Assert.Equal("ESTABLISHED", doc.RootElement.GetProperty("state").GetString());
        Assert.Equal("gw-1", doc.RootElement.GetProperty("gateway_call_id").GetString());
    }

    [Fact]
    public async Task Retries_server_errors_and_connection_failures_but_not_rejections()
    {
        var calls = 0;
        var flaky = new StubHandler(_ => ++calls < 3 ? new HttpResponseMessage(HttpStatusCode.BadGateway) : new HttpResponseMessage(HttpStatusCode.OK));
        await Client(flaky).SendAsync(Evt(), "https://api.example.com/events");
        Assert.Equal(3, flaky.Requests.Count);

        var rejected = new StubHandler(_ => new HttpResponseMessage(HttpStatusCode.Unauthorized));
        await Client(rejected).SendAsync(Evt(), "https://api.example.com/events");
        Assert.Single(rejected.Requests);

        var down = new StubHandler(_ => throw new HttpRequestException("connection refused"));
        await Client(down).SendAsync(Evt(), "https://api.example.com/events"); // must not throw
        Assert.Equal(3, down.Requests.Count);
    }
}
