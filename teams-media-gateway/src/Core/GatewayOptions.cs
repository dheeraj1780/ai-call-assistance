namespace CallCopilot.TeamsMediaGateway.Core;

/// <summary>Configuration. Bound from appsettings + environment (Gateway__AppSecret, ...).
/// Secrets (AppSecret, BackendSharedSecret) must come from the environment or a secret store.</summary>
public sealed class GatewayOptions
{
    /// <summary>Microsoft Entra application (bot) id of the Azure Bot with calling enabled.</summary>
    public string AppId { get; set; } = "";

    /// <summary>Client secret of that application (secret).</summary>
    public string AppSecret { get; set; } = "";

    /// <summary>Home tenant used for app tokens when a request carries no tenant.</summary>
    public string HomeTenantId { get; set; } = "";

    /// <summary>Public DNS name of THIS VM instance (must match the media certificate).</summary>
    public string ServiceDnsName { get; set; } = "";

    /// <summary>Thumbprint of the media TLS certificate in LocalMachine\My.</summary>
    public string CertificateThumbprint { get; set; } = "";

    /// <summary>Instance-level public IP of this VM (Microsoft requires one per instance).</summary>
    public string InstancePublicIPAddress { get; set; } = "";

    public int InstancePublicPort { get; set; } = 8445;
    public int InstanceInternalPort { get; set; } = 8445;

    /// <summary>Public https base URL Microsoft Graph calls for signalling ("/api/calling").</summary>
    public string CallbackBaseUrl { get; set; } = "";

    /// <summary>HMAC secret shared with the CallCopilot API (TEAMS_MEDIA_GATEWAY_SECRET, >= 32 chars).</summary>
    public string BackendSharedSecret { get; set; } = "";

    public int MaxConcurrentCalls { get; set; } = 4;

    /// <summary>Audio frames buffered per call while the API socket is slow/reconnecting
    /// (20 ms each: 250 = 5 s). Oldest frames are dropped beyond this (bounded memory).</summary>
    public int AudioQueueFrames { get; set; } = 250;

    /// <summary>Seconds between reconnect attempts of the API media socket (grows linearly).</summary>
    public double MediaReconnectBackoffSeconds { get; set; } = 1.0;

    public int MaxMediaReconnects { get; set; } = 10;
}

public static class GatewayOptionsValidator
{
    /// <summary>Problems that make the gateway refuse to start (insecure or unusable contract).</summary>
    public static List<string> Fatal(GatewayOptions o)
    {
        var problems = new List<string>();
        if (string.IsNullOrEmpty(o.BackendSharedSecret) || o.BackendSharedSecret.Length < 32)
            problems.Add("Gateway:BackendSharedSecret must be set (>= 32 characters)");
        if (o.MaxConcurrentCalls < 1) problems.Add("Gateway:MaxConcurrentCalls must be >= 1");
        if (o.AudioQueueFrames < 10) problems.Add("Gateway:AudioQueueFrames must be >= 10");
        return problems;
    }

    /// <summary>Problems that keep the Teams media platform from starting. The gateway still
    /// runs (health reports them) so the deployment can be diagnosed.</summary>
    public static List<string> Media(GatewayOptions o)
    {
        var problems = new List<string>();
        if (!Guid.TryParse(o.AppId, out _)) problems.Add("Gateway:AppId (Entra application id) is missing or not a GUID");
        if (string.IsNullOrEmpty(o.AppSecret)) problems.Add("Gateway:AppSecret is missing");
        if (string.IsNullOrEmpty(o.ServiceDnsName)) problems.Add("Gateway:ServiceDnsName is missing");
        if (string.IsNullOrEmpty(o.CertificateThumbprint)) problems.Add("Gateway:CertificateThumbprint is missing");
        if (!System.Net.IPAddress.TryParse(o.InstancePublicIPAddress, out var ip) || ip.Equals(System.Net.IPAddress.Any))
            problems.Add("Gateway:InstancePublicIPAddress must be this VM's instance-level public IP");
        if (!Uri.TryCreate(o.CallbackBaseUrl, UriKind.Absolute, out var cb) || cb.Scheme != Uri.UriSchemeHttps)
            problems.Add("Gateway:CallbackBaseUrl must be a public https URL");
        if (!OperatingSystem.IsWindows())
            problems.Add("Application-hosted media requires Windows (Windows Server in Azure for production)");
        return problems;
    }
}
