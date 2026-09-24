using System.Net;
using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using CallCopilot.TeamsMediaGateway;
using CallCopilot.TeamsMediaGateway.Core;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;

namespace TeamsMediaGateway.Tests;

public sealed class GatewayFactory(IMediaBot? bot = null) : WebApplicationFactory<Program>
{
    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseSetting("Gateway:BackendSharedSecret", TestData.Secret);
        if (bot is not null) builder.ConfigureTestServices(s => s.AddSingleton(bot));
    }
}

public class HttpEndpointTests
{
    private static HttpRequestMessage Signed(HttpMethod method, string path, object? payload = null, string secret = TestData.Secret)
    {
        var body = payload is null ? [] : JsonSerializer.SerializeToUtf8Bytes(payload);
        var (ts, sig) = Signing.Sign(secret, body);
        var req = new HttpRequestMessage(method, path) { Content = new ByteArrayContent(body) };
        req.Content.Headers.ContentType = new("application/json");
        req.Headers.Add(Signing.TimestampHeader, ts);
        req.Headers.Add(Signing.SignatureHeader, sig);
        return req;
    }

    [Fact]
    public async Task Starts_degraded_and_reports_why_media_is_unavailable()
    {
        // Real startup path (no fake bot): no Teams/Azure configuration on this machine.
        using var factory = new GatewayFactory();
        var client = factory.CreateClient();
        var alive = await client.GetAsync("/healthz");
        Assert.Equal(HttpStatusCode.OK, alive.StatusCode);
        var health = await client.SendAsync(Signed(HttpMethod.Get, "/health"));
        Assert.Equal(HttpStatusCode.ServiceUnavailable, health.StatusCode);
        using var doc = JsonDocument.Parse(await health.Content.ReadAsStringAsync());
        Assert.Equal("unavailable", doc.RootElement.GetProperty("status").GetString());
        var problems = doc.RootElement.GetProperty("problems").EnumerateArray().Select(p => p.GetString()!).ToList();
        Assert.Contains(problems, p => p.Contains("AppId"));
        Assert.Contains(problems, p => p.Contains("CertificateThumbprint"));
        var join = await client.SendAsync(Signed(HttpMethod.Post, "/v1/calls", TestData.Join()));
        Assert.Equal(HttpStatusCode.ServiceUnavailable, join.StatusCode);
        var notification = await client.PostAsync("/api/calling", new StringContent("{}"));
        Assert.Equal(HttpStatusCode.ServiceUnavailable, notification.StatusCode);
    }

    [Fact]
    public void Refuses_to_start_without_shared_secret()
    {
        var ex = Assert.Throws<InvalidOperationException>(() => GatewayApp.Build(["--environment=Development"]));
        Assert.Contains("BackendSharedSecret", ex.Message);
    }

    [Fact]
    public async Task Api_endpoints_require_a_valid_signature()
    {
        var bot = new FakeBot();
        using var factory = new GatewayFactory(bot);
        var client = factory.CreateClient();
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.GetAsync("/health")).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.PostAsJsonAsync("/v1/calls", TestData.Join())).StatusCode);
        var forged = Signed(HttpMethod.Post, "/v1/calls", TestData.Join(), secret: "wrong-secret-0123456789abcdefghijklmn");
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.SendAsync(forged)).StatusCode);
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.DeleteAsync("/v1/calls/gw-1")).StatusCode);
        Assert.Empty(bot.Joined);
        Assert.Empty(bot.Left);
    }

    [Fact]
    public async Task Join_and_leave_through_the_signed_contract()
    {
        var bot = new FakeBot();
        using var factory = new GatewayFactory(bot);
        var client = factory.CreateClient();
        var health = await client.SendAsync(Signed(HttpMethod.Get, "/health"));
        Assert.Equal(HttpStatusCode.OK, health.StatusCode);
        var join = await client.SendAsync(Signed(HttpMethod.Post, "/v1/calls", TestData.Join()));
        Assert.Equal(HttpStatusCode.OK, join.StatusCode);
        using (var doc = JsonDocument.Parse(await join.Content.ReadAsStringAsync()))
            Assert.Equal("gw-1", doc.RootElement.GetProperty("gateway_call_id").GetString());
        Assert.Equal("sales-aad", Assert.Single(bot.Joined).SalespersonAadId);
        var leave = await client.SendAsync(Signed(HttpMethod.Delete, "/v1/calls/gw-1"));
        Assert.Equal(HttpStatusCode.NoContent, leave.StatusCode);
        Assert.Equal(["gw-1"], bot.Left);
    }

    [Fact]
    public async Task Rejects_invalid_join_requests()
    {
        var bot = new FakeBot();
        using var factory = new GatewayFactory(bot);
        var client = factory.CreateClient();
        Assert.Equal(HttpStatusCode.BadRequest, (await client.SendAsync(Signed(HttpMethod.Post, "/v1/calls", TestData.Join(receiveOnly: false)))).StatusCode);
        var garbage = Signed(HttpMethod.Post, "/v1/calls");
        garbage.Content = new ByteArrayContent(Encoding.UTF8.GetBytes("not json"));
        var (ts, sig) = Signing.Sign(TestData.Secret, Encoding.UTF8.GetBytes("not json"));
        garbage.Headers.Remove(Signing.TimestampHeader); garbage.Headers.Remove(Signing.SignatureHeader);
        garbage.Headers.Add(Signing.TimestampHeader, ts); garbage.Headers.Add(Signing.SignatureHeader, sig);
        Assert.Equal(HttpStatusCode.BadRequest, (await client.SendAsync(garbage)).StatusCode);
        bot.JoinError = new ArgumentException("bad link");
        Assert.Equal(HttpStatusCode.BadRequest, (await client.SendAsync(Signed(HttpMethod.Post, "/v1/calls", TestData.Join()))).StatusCode);
        bot.JoinError = new GatewayAtCapacityException();
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await client.SendAsync(Signed(HttpMethod.Post, "/v1/calls", TestData.Join()))).StatusCode);
        Assert.Empty(bot.Joined);
    }

    [Fact]
    public async Task Graph_notifications_are_passed_to_the_sdk_when_ready()
    {
        using var factory = new GatewayFactory(new FakeBot());
        var resp = await factory.CreateClient().PostAsync("/api/calling", new StringContent("{}"));
        Assert.Equal(HttpStatusCode.Accepted, resp.StatusCode);
    }

    [Fact]
    public async Task Sdk_validator_rejects_notifications_without_microsoft_token()
    {
        // Microsoft's own DefaultAuthenticationProvider (used via SetAuthentication) - no network needed
        // to reject a request that carries no bearer token.
        var provider = new Microsoft.Graph.Communications.Client.Authentication.DefaultAuthenticationProvider(
            Guid.NewGuid().ToString(), new StaticTokenProvider(), new Microsoft.Graph.Communications.Common.Telemetry.GraphLogger("test"));
        using var request = new HttpRequestMessage(HttpMethod.Post, "https://media.example.com/api/calling") { Content = new StringContent("{}") };
        var result = await provider.ValidateInboundRequestAsync(request);
        Assert.False(result.IsValid);
    }

    private sealed class StaticTokenProvider : Microsoft.Graph.Communications.Client.Authentication.ITokenProvider
    {
        public Task<string> AcquireTokenAsync(string tenantId) => Task.FromResult("unused");
    }
}

/// <summary>Real WebSocket sink against a local in-process WebSocket server that speaks like the API.</summary>
public class WebSocketSinkTests
{
    [Fact]
    public async Task Sends_start_media_and_stop_in_the_api_format()
    {
        var received = new List<string>();
        var done = new TaskCompletionSource();
        var builder = WebApplication.CreateSlimBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        await using var server = builder.Build();
        server.UseWebSockets();
        server.Map("/media", async (HttpContext ctx) =>
        {
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            var buffer = new byte[64 * 1024];
            while (true)
            {
                var r = await ws.ReceiveAsync(buffer, CancellationToken.None);
                if (r.MessageType == WebSocketMessageType.Close) break;
                received.Add(Encoding.UTF8.GetString(buffer, 0, r.Count));
            }
            done.TrySetResult();
        });
        await server.StartAsync();
        var address = server.Urls.First().Replace("http://", "ws://");

        var pipeline = new AudioPipeline(new WebSocketMediaSink(new Uri($"{address}/media?token=t")), 50, NullLogger.Instance);
        await pipeline.StartAsync(CancellationToken.None);
        var pcm = TestData.Pcm();
        pipeline.TryWrite(new AudioFrame("agent", pcm));
        pipeline.TryWrite(new AudioFrame("customer", pcm));
        await pipeline.StopAsync(TimeSpan.FromSeconds(5));
        await done.Task.WaitAsync(TimeSpan.FromSeconds(5));

        Assert.Equal(4, received.Count);
        using (var start = JsonDocument.Parse(received[0]))
        {
            Assert.Equal("start", start.RootElement.GetProperty("event").GetString());
            Assert.Equal("linear16", start.RootElement.GetProperty("format").GetProperty("encoding").GetString());
            Assert.Equal(16000, start.RootElement.GetProperty("format").GetProperty("sample_rate").GetInt32());
        }
        using (var media = JsonDocument.Parse(received[1]))
        {
            Assert.Equal("media", media.RootElement.GetProperty("event").GetString());
            Assert.Equal("agent", media.RootElement.GetProperty("track").GetString());
            Assert.Equal(1, media.RootElement.GetProperty("seq").GetInt64());
            Assert.Equal(pcm, Convert.FromBase64String(media.RootElement.GetProperty("payload").GetString()!));
        }
        using (var stop = JsonDocument.Parse(received[3]))
            Assert.Equal("stop", stop.RootElement.GetProperty("event").GetString());
    }

    [Fact]
    public async Task Unreachable_api_is_a_connect_failure()
    {
        var sink = new WebSocketMediaSink(new Uri("ws://127.0.0.1:1/media"));
        await Assert.ThrowsAnyAsync<Exception>(() => sink.ConnectAsync(new CancellationTokenSource(TimeSpan.FromSeconds(5)).Token));
    }
}
