using System.Text.Json;
using CallCopilot.TeamsMediaGateway;
using Microsoft.Graph.Communications.Common.Transport;

// Teams media gateway: the ONLY component that touches Teams real-time media.
// The CallCopilot FastAPI service stays the business/API service; this process joins meetings,
// receives audio in memory and streams it to the API. NOT BUILT / NOT RUN (see README.md).

var builder = WebApplication.CreateBuilder(args);
var options = builder.Configuration.GetSection("Gateway").Get<GatewayOptions>() ?? new GatewayOptions();
if (string.IsNullOrEmpty(options.AppSecret) || string.IsNullOrEmpty(options.BackendSharedSecret))
    throw new InvalidOperationException("Gateway__AppSecret and Gateway__BackendSharedSecret must be set");

builder.Services.AddSingleton(options);
builder.Services.AddHttpClient<BackendClient>(c => c.Timeout = TimeSpan.FromSeconds(10));
builder.Services.AddSingleton<BotService>();
builder.WebHost.ConfigureKestrel(k => k.ListenAnyIP(options.CallSignalingPort, l => l.UseHttps()));

var app = builder.Build();
var bot = app.Services.GetRequiredService<BotService>();
bot.Initialize();

async Task<byte[]?> VerifiedBody(HttpRequest req)
{
    using var ms = new MemoryStream();
    await req.Body.CopyToAsync(ms);
    var body = ms.ToArray();
    return Signing.Verify(options.BackendSharedSecret, req.Headers["X-CC-Timestamp"], req.Headers["X-CC-Signature"], body)
        ? body
        : null;
}

// Liveness + capacity (called by the API's connection test, signed).
app.MapGet("/health", async (HttpRequest req) =>
    await VerifiedBody(req) is null
        ? Results.Unauthorized()
        : Results.Ok(new { status = "ok", active_calls = bot.ActiveCalls }));

// Join a Teams meeting (from the CallCopilot API).
app.MapPost("/v1/calls", async (HttpRequest req, CancellationToken ct) =>
{
    var body = await VerifiedBody(req);
    if (body is null) return Results.Unauthorized();
    var join = JsonSerializer.Deserialize<JoinRequest>(body);
    if (join is null || !join.ReceiveOnly) return Results.BadRequest();
    try
    {
        var id = await bot.JoinAsync(join, ct);
        return Results.Ok(new { gateway_call_id = id });
    }
    catch (InvalidOperationException)
    {
        return Results.StatusCode(503); // at capacity
    }
    catch (ArgumentException)
    {
        return Results.BadRequest(new { error = new { code = "unsupported_join_url" } });
    }
});

// Leave a meeting (from the CallCopilot API).
app.MapDelete("/v1/calls/{id}", async (string id, HttpRequest req) =>
{
    if (await VerifiedBody(req) is null) return Results.Unauthorized();
    await bot.LeaveAsync(id);
    return Results.NoContent();
});

// Microsoft Graph call-signalling notifications (authenticated by Microsoft's JWT inside the SDK).
app.MapPost("/api/calling", async (HttpContext ctx) =>
{
    using var request = await ctx.Request.ToHttpRequestMessageAsync();
    var response = await bot.Client.ProcessNotificationAsync(request).ConfigureAwait(false);
    ctx.Response.StatusCode = (int)response.StatusCode;
});

app.Lifetime.ApplicationStopping.Register(() => bot.DisposeAsync().AsTask().GetAwaiter().GetResult());
app.Run();

internal static class RequestExtensions
{
    public static async Task<HttpRequestMessage> ToHttpRequestMessageAsync(this HttpRequest req)
    {
        var message = new HttpRequestMessage(new HttpMethod(req.Method), $"{req.Scheme}://{req.Host}{req.Path}{req.QueryString}");
        using var ms = new MemoryStream();
        await req.Body.CopyToAsync(ms);
        message.Content = new ByteArrayContent(ms.ToArray());
        foreach (var h in req.Headers)
            if (!message.Headers.TryAddWithoutValidation(h.Key, h.Value.ToArray()))
                message.Content.Headers.TryAddWithoutValidation(h.Key, h.Value.ToArray());
        return message;
    }
}
