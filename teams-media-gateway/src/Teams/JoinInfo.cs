using System.Text.Json;
using System.Text.RegularExpressions;
using System.Web;
using Microsoft.Graph.Models;

namespace CallCopilot.TeamsMediaGateway.Teams;

/// <summary>Parses a classic Teams meeting join link ("/l/meetup-join/...") into Graph ChatInfo +
/// OrganizerMeetingInfo, as Microsoft's samples do. Newer short links ("/meet/&lt;id&gt;?p=") must be
/// resolved through Graph onlineMeetings first (not implemented).</summary>
public static partial class JoinInfo
{
    [GeneratedRegex(@"^https://teams\.(microsoft|live)\.com/l/meetup-join/(?<thread>[^/]+)/(?<message>[^/?]+)\?context=(?<context>\{.*\})", RegexOptions.CultureInvariant)]
    private static partial Regex MeetupJoin();

    public static (ChatInfo Chat, OrganizerMeetingInfo Meeting, string? TenantId) Parse(string joinUrl)
    {
        var decoded = HttpUtility.UrlDecode(joinUrl ?? "");
        var match = MeetupJoin().Match(decoded);
        if (!match.Success)
            throw new ArgumentException("Unsupported Teams meeting link format (expected /l/meetup-join/...)", nameof(joinUrl));
        JsonElement context;
        try
        {
            context = JsonDocument.Parse(match.Groups["context"].Value).RootElement;
        }
        catch (JsonException)
        {
            throw new ArgumentException("Teams meeting link has an invalid context", nameof(joinUrl));
        }
        var tenantId = context.TryGetProperty("Tid", out var tid) ? tid.GetString() : null;
        var organizerId = context.TryGetProperty("Oid", out var oid) ? oid.GetString() : null;
        if (string.IsNullOrEmpty(organizerId))
            throw new ArgumentException("Teams meeting link has no organizer id", nameof(joinUrl));
        var chat = new ChatInfo
        {
            ThreadId = match.Groups["thread"].Value,
            MessageId = match.Groups["message"].Value,
            ReplyChainMessageId = context.TryGetProperty("MessageId", out var reply) ? reply.GetString() : null,
        };
        var organizer = new Identity { Id = organizerId };
        if (!string.IsNullOrEmpty(tenantId)) organizer.AdditionalData = new Dictionary<string, object> { ["tenantId"] = tenantId };
        var meeting = new OrganizerMeetingInfo { Organizer = new IdentitySet { User = organizer } };
        return (chat, meeting, tenantId);
    }
}
