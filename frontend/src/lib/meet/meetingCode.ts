/** Google Meet link or code -> normalised meeting code "abc-defg-hij" (mirrors the backend). */
const CODE = /^[a-z]{3}-?[a-z]{4}-?[a-z]{3}$/;

export function parseMeetingCode(value: string): string | null {
  let text = (value ?? "").trim();
  if (!text || text.length > 2000) return null;
  if (text.includes("/") || text.includes(":")) {
    let url: URL;
    try {
      url = new URL(text.includes("://") ? text : `https://${text}`);
    } catch {
      return null;
    }
    if (url.protocol !== "https:" || url.hostname.toLowerCase() !== "meet.google.com") return null;
    const path = url.pathname.replace(/^\/+|\/+$/g, "");
    if (path.includes("/")) return null;
    text = path;
  }
  const code = text.toLowerCase();
  if (!CODE.test(code)) return null;
  const letters = code.replace(/-/g, "");
  return `${letters.slice(0, 3)}-${letters.slice(3, 7)}-${letters.slice(7)}`;
}

/** User-facing explanations for the API's MEET_* error codes. */
export const MEET_ERROR_HELP: Record<string, string> = {
  MEET_MEDIA_API_NOT_ELIGIBLE:
    "Google refused live meeting media. The Meet Media API is a Developer Preview: the Google Cloud project, your Google account and every participant must be enrolled.",
  MEET_CONSENT_REQUIRED:
    "No one who can approve the copilot is in the meeting. For a Gmail meeting, the person who started it must be present.",
  MEET_CONFERENCE_NOT_ACTIVE: "The meeting has not started yet. Join the Google Meet first, then start the copilot.",
  MEET_CONNECTIONS_EXHAUSTED: "Another app is already receiving this meeting's media. Wait about 30 seconds and retry.",
  MEET_INCOMPATIBLE_PARTICIPANT: "A participant's account or device is not compatible with the Meet Media API.",
  MEET_DISABLED: "Media access is disabled for this meeting (host, admin, watermarking or encryption).",
  MEET_MEETING_NOT_FOUND: "This meeting does not exist or your Google account cannot see it.",
  MEET_SCOPE_MISSING: "Google Meet permissions were not granted. Reconnect Google Meet in Settings > Integrations.",
  MEET_AUTH_EXPIRED: "Your Google sign-in expired. Reconnect Google Meet in Settings > Integrations.",
  google_meet_not_connected: "Connect your Google account for Meet first (Settings > Integrations).",
  MEET_INVALID_OFFER: "Your browser could not create a compatible connection. Use a current Chrome or Edge.",
  MEET_MOCK_MODE: "Google Meet is in mock mode: no real meeting audio can be received.",
};
