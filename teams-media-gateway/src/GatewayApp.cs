using System.Text.Json;
using CallCopilot.TeamsMediaGateway.Core;
using CallCopilot.TeamsMediaGateway.Teams;

namespace CallCopilot.TeamsMediaGateway;

/// <summary>HTTP surface of the gateway. Business logic, STT, AI, CRM and persistence live in the
/// CallCopilot API; this service only bridges Teams media to it.</summary>
public static class GatewayApp
{
    /// <summary>Prefix of the environment variables only this host reads
    /// ("CCGW_Kestrel__Endpoints__Https__Url" -> "Kestrel:Endpoints:Https:Url"). The Teams media SDK
    /// starts its own default-configured ASP.NET host in this process, which reads every UNPREFIXED
    /// variable: an unprefixed Kestrel__Endpoints__* makes it bind our endpoints too and media
    /// initialization fails ("Failed to bind to address https://[::]:443: address already in use").
    /// Listener settings therefore come only through this namespace (deploy-azure-vm.ps1).</summary>
    public const string EnvironmentPrefix = "CCGW_";

    /// <summary>Adds the gateway-only environment namespace (see <see cref="EnvironmentPrefix"/>).</summary>
    public static IConfigurationBuilder AddGatewayEnvironment(IConfigurationBuilder configuration) =>
        configuration.AddEnvironmentVariables(EnvironmentPrefix);

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
        // Listener (Kestrel) settings come from CCGW_-prefixed variables, which the media SDK's
        // internal host does not read. Added before anything reads configuration.
        AddGatewayEnvironment(builder.Configuration);
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
            // The SDK wraps the real cause (e.g. DllNotFoundException 'NativeMedia' 0x8007007E) in a
            // ServiceException: log the whole chain. Types, HRESULTs and messages only; configured
            // secrets are redacted in case a message ever echoes one.
            logger.LogError("teams_media_platform_init_failed chain={Chain}",
                DescribeExceptionChain(ex, options.AppSecret, options.BackendSharedSecret));
            return new UnavailableMediaBot([reason]);
        }
    }

    /// <summary>"[0] Type (HRESULT 0x...): message --> [1] ..." for an exception and its inner
    /// exceptions (all children of an AggregateException), bounded in depth and length. No stack
    /// traces or data; any non-trivial <paramref name="secrets"/> value is replaced by "&lt;redacted&gt;".</summary>
    internal static string DescribeExceptionChain(Exception exception, params string?[] secrets)
    {
        const int MaxDepth = 8, MaxMessage = 500;
        var redact = secrets.Where(s => !string.IsNullOrEmpty(s) && s.Length >= 8).Select(s => s!).ToArray();
        var parts = new List<string>();
        var seen = new HashSet<Exception>(ReferenceEqualityComparer.Instance);

        void Walk(Exception e, int depth)
        {
            if (depth > MaxDepth || !seen.Add(e)) return;
            var message = (e.Message ?? "").ReplaceLineEndings(" ");
            foreach (var s in redact) message = message.Replace(s, "<redacted>", StringComparison.Ordinal);
            if (message.Length > MaxMessage) message = message[..MaxMessage] + "...";
            parts.Add($"[{depth}] {e.GetType().FullName} (HRESULT 0x{e.HResult:X8}): {message}");
            if (e is AggregateException aggregate)
                foreach (var inner in aggregate.InnerExceptions) Walk(inner, depth + 1);
            else if (e.InnerException is not null)
                Walk(e.InnerException, depth + 1);
        }

        Walk(exception, 0);
        return string.Join(" --> ", parts);
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
