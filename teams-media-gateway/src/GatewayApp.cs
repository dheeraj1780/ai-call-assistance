using System.Text.Json;
using CallCopilot.TeamsMediaGateway.Core;
using CallCopilot.TeamsMediaGateway.Teams;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>HTTP surface of the gateway. Business logic, STT, AI, CRM and persistence live in the
/// CallCopilot API; this service only bridges Teams media to it.</summary>
public static class GatewayApp
{
    public static WebApplication Build(string[] args, Action<WebApplicationBuilder>? configure = null)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = args,
            // As a Windows service the working directory is System32: read appsettings.json from
            // the install directory instead.
            ContentRootPath = Microsoft.Extensions.Hosting.WindowsServices.WindowsServiceHelpers.IsWindowsService()
                ? AppContext.BaseDirectory
                : null,
        });
        // Reports start/stop to the Windows Service Control Manager when run as a service
        // (graceful stop: active calls are left). No effect otherwise.
        builder.Host.UseWindowsService(o => o.ServiceName = "CallCopilotTeamsMediaGateway");
        var options = builder.Configuration.GetSection("Gateway").Get<GatewayOptions>() ?? new GatewayOptions();
        var fatal = GatewayOptionsValidator.Fatal(options);
        if (fatal.Count > 0)
            throw new InvalidOperationException("Invalid gateway configuration: " + string.Join("; ", fatal));

        builder.Services.AddSingleton(options);
        builder.Services.AddHttpClient<BackendClient>(c => c.Timeout = TimeSpan.FromSeconds(10));
        builder.Services.AddSingleton<ICallEventSink>(sp => sp.GetRequiredService<BackendClient>());
        builder.Services.AddSingleton<IMediaBot>(sp => CreateBot(options, sp.GetRequiredService<ICallEventSink>(), sp.GetRequiredService<ILoggerFactory>()));
        configure?.Invoke(builder);

        var app = builder.Build();
        MapEndpoints(app, options);
        return app;
    }

    /// <summary>Starts the Teams media platform, or a degraded bot that reports why it cannot.</summary>
    public static IMediaBot CreateBot(GatewayOptions options, ICallEventSink events, ILoggerFactory loggers)
    {
        var logger = loggers.CreateLogger("CallCopilot.TeamsMediaGateway");
        var problems = GatewayOptionsValidator.Media(options);
        if (problems.Count > 0)
        {
            logger.LogWarning("teams_media_unavailable reasons={Reasons}", string.Join("; ", problems));
            return new UnavailableMediaBot(problems);
        }
        try
        {
            var bot = BotService.Create(options, events, loggers);
            logger.LogInformation("teams_media_platform_started");
            return bot;
        }
        catch (Exception ex)
        {
            // Report the failure honestly (e.g. certificate not found, unsupported OS image).
            var reason = $"Media platform failed to start: {ex.GetType().Name}: {ex.Message}";
            logger.LogError("teams_media_unavailable reasons={Reasons}", reason);
            return new UnavailableMediaBot([reason]);
        }
    }

    private static async Task<byte[]?> VerifiedBody(HttpRequest req, GatewayOptions options)
    {
        using var ms = new MemoryStream();
        await req.Body.CopyToAsync(ms).ConfigureAwait(false);
        var body = ms.ToArray();
        return Signing.Verify(options.BackendSharedSecret, req.Headers[Signing.TimestampHeader], req.Headers[Signing.SignatureHeader], body)
            ? body
            : null;
    }

    private static void MapEndpoints(WebApplication app, GatewayOptions options)
    {
        // Unauthenticated liveness for load balancers: no details.
        app.MapGet("/healthz", () => Results.Ok(new { status = "alive" }));

        // Readiness + reasons (signed; used by the API's connection test).
        app.MapGet("/health", async (HttpRequest req, IMediaBot bot) =>
        {
            if (await VerifiedBody(req, options) is null) return Results.Unauthorized();
            var status = bot.Status;
            var body = new { status = status.Ready ? "ok" : "unavailable", media_platform = status.Ready, problems = status.Problems, active_calls = bot.ActiveCalls };
            return status.Ready ? Results.Ok(body) : Results.Json(body, statusCode: StatusCodes.Status503ServiceUnavailable);
        });

        app.MapPost("/v1/calls", async (HttpRequest req, IMediaBot bot, CancellationToken ct) =>
        {
            var body = await VerifiedBody(req, options);
            if (body is null) return Results.Unauthorized();
            JoinRequest? join;
            try { join = JsonSerializer.Deserialize<JoinRequest>(body); }
            catch (JsonException) { join = null; }
            if (join is null) return Results.BadRequest(new { error = new { code = "invalid_request" } });
            var problems = join.Problems().ToList();
            if (problems.Count > 0) return Results.BadRequest(new { error = new { code = "invalid_request", fields = problems } });
            try
            {
                return Results.Ok(new { gateway_call_id = await bot.JoinAsync(join, ct) });
            }
            catch (MediaBotUnavailableException)
            {
                return Results.Json(new { error = new { code = "media_unavailable" } }, statusCode: 503);
            }
            catch (GatewayAtCapacityException)
            {
                return Results.Json(new { error = new { code = "at_capacity" } }, statusCode: 503);
            }
            catch (ArgumentException)
            {
                return Results.BadRequest(new { error = new { code = "unsupported_join_url" } });
            }
        });

        app.MapDelete("/v1/calls/{id}", async (string id, HttpRequest req, IMediaBot bot) =>
        {
            if (await VerifiedBody(req, options) is null) return Results.Unauthorized();
            await bot.LeaveAsync(id);
            return Results.NoContent();
        });

        // Microsoft Graph call-signalling notifications; the SDK validates Microsoft's JWT through
        // AuthenticationProvider.ValidateInboundRequestAsync.
        app.MapPost("/api/calling", async (HttpContext ctx, IMediaBot bot) =>
        {
            if (!bot.Status.Ready) return Results.StatusCode(StatusCodes.Status503ServiceUnavailable);
            using var request = await ToHttpRequestMessageAsync(ctx.Request);
            using var response = await bot.ProcessNotificationAsync(request);
            return Results.StatusCode((int)response.StatusCode);
        });

        app.Lifetime.ApplicationStopping.Register(() =>
        {
            if (app.Services.GetService<IMediaBot>() is IAsyncDisposable d) d.DisposeAsync().AsTask().GetAwaiter().GetResult();
        });
    }

    private static async Task<HttpRequestMessage> ToHttpRequestMessageAsync(HttpRequest req)
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
