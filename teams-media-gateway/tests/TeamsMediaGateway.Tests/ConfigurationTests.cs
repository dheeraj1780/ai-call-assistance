using System.Text.RegularExpressions;
using CallCopilot.TeamsMediaGateway;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Configuration.EnvironmentVariables;

namespace TeamsMediaGateway.Tests;

// These tests set process environment variables: never run them in parallel with each other.
[CollectionDefinition("ProcessEnvironment", DisableParallelization = true)]
public sealed class ProcessEnvironmentCollection;

/// <summary>The listener configuration must reach ONLY the gateway host. The Teams media SDK starts its
/// own default ASP.NET host in the same process, which reads every unprefixed environment variable; an
/// unprefixed Kestrel__Endpoints__* made it bind :443 and media initialization failed.</summary>
[Collection("ProcessEnvironment")]
public class GatewayConfigurationTests
{
    private static readonly string[] Keys =
    [
        "Kestrel__Endpoints__Loopback__Url", "Kestrel__Endpoints__Https__Url", "Kestrel__Endpoints__Https__Certificate__Subject",
        "Kestrel__Endpoints__Https__Certificate__Store", "Kestrel__Endpoints__Https__Certificate__Location",
        "Kestrel__Endpoints__Https__Certificate__AllowInvalid",
    ];

    private static void WithEnvironment(IReadOnlyDictionary<string, string> values, Action body)
    {
        var names = values.Keys.Concat(Keys).Concat(Keys.Select(k => GatewayApp.EnvironmentPrefix + k)).Distinct().ToList();
        var saved = names.ToDictionary(n => n, Environment.GetEnvironmentVariable);
        try
        {
            foreach (var n in names) Environment.SetEnvironmentVariable(n, null);
            foreach (var (k, v) in values) Environment.SetEnvironmentVariable(k, v);
            body();
        }
        finally
        {
            foreach (var (k, v) in saved) Environment.SetEnvironmentVariable(k, v);
        }
    }

    private static Dictionary<string, string> DeployedListenerSettings(string prefix) => new()
    {
        [prefix + "Kestrel__Endpoints__Loopback__Url"] = "http://127.0.0.1:9441",
        [prefix + "Kestrel__Endpoints__Https__Url"] = "https://*:443",
        [prefix + "Kestrel__Endpoints__Https__Certificate__Subject"] = "media.example.com",
        [prefix + "Kestrel__Endpoints__Https__Certificate__Store"] = "My",
        [prefix + "Kestrel__Endpoints__Https__Certificate__Location"] = "LocalMachine",
        [prefix + "Kestrel__Endpoints__Https__Certificate__AllowInvalid"] = "false",
    };

    [Fact]
    public void Prefixed_variables_map_to_the_kestrel_section_without_unprefixed_ones()
    {
        // A + B + C: exactly what deploy-azure-vm.ps1 writes resolves to the same Kestrel section,
        // with no unprefixed Kestrel__* present anywhere in the process.
        WithEnvironment(DeployedListenerSettings(GatewayApp.EnvironmentPrefix), () =>
        {
            Assert.All(Keys, k => Assert.Null(Environment.GetEnvironmentVariable(k)));
            var config = GatewayApp.AddGatewayEnvironment(new ConfigurationBuilder()).Build();
            Assert.Equal("https://*:443", config["Kestrel:Endpoints:Https:Url"]);
            Assert.Equal("http://127.0.0.1:9441", config["Kestrel:Endpoints:Loopback:Url"]);
            Assert.Equal("media.example.com", config["Kestrel:Endpoints:Https:Certificate:Subject"]);
            Assert.Equal("My", config["Kestrel:Endpoints:Https:Certificate:Store"]);
            Assert.Equal("LocalMachine", config["Kestrel:Endpoints:Https:Certificate:Location"]);
            Assert.Equal("false", config["Kestrel:Endpoints:Https:Certificate:AllowInvalid"]);
            Assert.Null(config["CCGW_Kestrel:Endpoints:Https:Url"]);
        });
    }

    [Fact]
    public void A_default_host_does_not_see_the_prefixed_listener_settings()
    {
        // What the media SDK's internal host (CreateDefaultBuilder) loads: plain environment variables.
        WithEnvironment(DeployedListenerSettings(GatewayApp.EnvironmentPrefix), () =>
        {
            var defaultHostConfig = new ConfigurationBuilder().AddEnvironmentVariables().Build();
            Assert.Null(defaultHostConfig["Kestrel:Endpoints:Https:Url"]);
            Assert.Empty(defaultHostConfig.GetSection("Kestrel").GetChildren());
        });
    }

    [Fact]
    public async Task Gateway_host_binds_its_listener_from_the_prefixed_namespace()
    {
        // C: the real host (GatewayApp.Build + Kestrel) starts its loopback listener from CCGW_ only.
        WebApplication? app = null;
        WithEnvironment(new Dictionary<string, string> { ["CCGW_Kestrel__Endpoints__Loopback__Url"] = "http://127.0.0.1:0" }, () =>
        {
            app = GatewayApp.Build(["--Gateway:BackendSharedSecret=" + TestData.Secret]);
        });
        await using var running = app!;
        Assert.Equal("http://127.0.0.1:0", running.Configuration["Kestrel:Endpoints:Loopback:Url"]);
        await running.StartAsync();
        try
        {
            var url = Assert.Single(running.Urls);
            Assert.StartsWith("http://127.0.0.1:", url);
            using var http = new HttpClient();
            Assert.Equal(System.Net.HttpStatusCode.OK, (await http.GetAsync(url + "/healthz")).StatusCode);
        }
        finally { await running.StopAsync(); }
    }

    [Fact]
    public void Regression_gateway_reads_environment_through_the_CCGW_namespace()
    {
        var app = GatewayApp.Build(["--Gateway:BackendSharedSecret=" + TestData.Secret]);
        var sources = ((IConfigurationBuilder)app.Configuration).Sources;
        Assert.Contains(sources, s => s is EnvironmentVariablesConfigurationSource e && e.Prefix == "CCGW_");
        Assert.Equal("CCGW_", GatewayApp.EnvironmentPrefix);
    }

    [Fact]
    public void Regression_deploy_script_never_writes_unprefixed_kestrel_variables()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "deploy-azure-vm.ps1"))) dir = dir.Parent;
        Assert.NotNull(dir);
        var script = File.ReadAllText(Path.Combine(dir!.FullName, "deploy-azure-vm.ps1"));
        // Every Kestrel variable the script WRITES ("Name" = value / $envMap["Name"] =) is CCGW_-prefixed.
        var written = Regex.Matches(script, @"(?:\$envMap\[|^\s*)""([A-Za-z_]*Kestrel__[A-Za-z_]+)""\s*\]?\s*=", RegexOptions.Multiline)
            .Select(m => m.Groups[1].Value).ToList();
        Assert.Equal(6, written.Count);
        Assert.All(written, name => Assert.StartsWith("CCGW_Kestrel__Endpoints__", name));
    }
}
