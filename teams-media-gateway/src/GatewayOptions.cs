namespace CallCopilot.TeamsMediaGateway;

/// <summary>Configuration. Bind from appsettings + environment (Gateway__AppSecret, ...).</summary>
public sealed class GatewayOptions
{
    /// <summary>Microsoft Entra application (bot) id registered as an Azure Bot with calling enabled.</summary>
    public string AppId { get; set; } = "";

    /// <summary>Client secret of that application. Secret: environment / Key Vault only.</summary>
    public string AppSecret { get; set; } = "";

    /// <summary>Public DNS name of THIS VM instance (matches the media TLS certificate).</summary>
    public string ServiceDnsName { get; set; } = "";

    /// <summary>Thumbprint of the TLS certificate (LocalMachine\My) used by the media platform.</summary>
    public string CertificateThumbprint { get; set; } = "";

    /// <summary>Instance-level public IP of this VM (Microsoft requires an ILPIP per instance).</summary>
    public string InstancePublicIPAddress { get; set; } = "";

    public int InstancePublicPort { get; set; } = 8445;
    public int InstanceInternalPort { get; set; } = 8445;
    public int CallSignalingPort { get; set; } = 9441;

    /// <summary>Public https base URL Microsoft Graph calls for call signalling (/api/calling).</summary>
    public string CallbackBaseUrl { get; set; } = "";

    /// <summary>HMAC secret shared with the CallCopilot API (TEAMS_MEDIA_GATEWAY_SECRET).</summary>
    public string BackendSharedSecret { get; set; } = "";

    public int MaxConcurrentCalls { get; set; } = 4;
}
