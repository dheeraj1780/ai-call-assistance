/** Integrations API. The backend is authoritative for which providers exist, which
 * capabilities they offer and what is configured: nothing here is provider-specific. */
import { apiFetch } from "./api";

export type IntegrationStatus = "NOT_CONFIGURED" | "CONFIGURED" | "VALIDATED" | "ENABLED" | "ERROR";
export type CapabilityState = IntegrationStatus | "NOT_AVAILABLE";

export interface IntegrationCapability {
  capability: string;
  label: string;
  description: string;
  channel: string;
  state: CapabilityState;
  enabled: boolean;
  available: boolean;
  reasons: string[];
}

export interface Integration {
  provider: string;
  slug: string;
  name: string;
  subtitle: string;
  channel: string;
  mode: "LIVE" | "MOCK";
  status: IntegrationStatus;
  configured: boolean;
  validated: boolean;
  enabled: boolean;
  capabilities: IntegrationCapability[];
  last_tested_at: string | null;
  error_state: string | null;
  mock_allowed: boolean;
}

export interface IntegrationField {
  key: string;
  label: string;
  kind: "text" | "secret" | "select";
  required: boolean;
  help: string;
  pattern: string | null;
  max_length: number;
  options: { value: string; label: string }[];
  /** Only for non-secret fields. Secrets are write-only and never returned. */
  value: string | null;
  is_set: boolean;
  display: string | null;
}

export interface Requirement {
  key: string;
  label: string;
  ok: boolean;
  scope: "company" | "server" | "user" | "provider";
  capability: string | null;
  hint: string | null;
}

export interface TestCheck {
  key: string;
  label: string;
  ok: boolean;
  detail: string | null;
  required: boolean;
}

export interface IntegrationDetail extends Integration {
  fields: IntegrationField[];
  requirements: Requirement[];
  last_test: {
    ok: boolean;
    checks: TestCheck[];
    missing_permissions: string[];
    account_label: string | null;
  } | null;
  setup: Record<string, string | string[] | null>;
  docs: string;
}

export interface ConfigureInput {
  mode: "LIVE" | "MOCK";
  values: Record<string, string | null>;
  clear_secrets?: string[];
}

export const integrationsApi = {
  list: () => apiFetch<Integration[]>("/api/v1/integrations"),
  get: (slug: string) => apiFetch<IntegrationDetail>(`/api/v1/integrations/${slug}`),
  configure: (slug: string, body: ConfigureInput) =>
    apiFetch<IntegrationDetail>(`/api/v1/integrations/${slug}/config`, { method: "PUT", body }),
  test: (slug: string) =>
    apiFetch<IntegrationDetail>(`/api/v1/integrations/${slug}/test`, { method: "POST" }),
  setCapability: (slug: string, capability: string, enable: boolean) =>
    apiFetch<IntegrationDetail>(
      `/api/v1/integrations/${slug}/capabilities/${capability}/${enable ? "enable" : "disable"}`,
      { method: "POST" },
    ),
  disconnect: (slug: string) => apiFetch<void>(`/api/v1/integrations/${slug}`, { method: "DELETE" }),

  teamsConnection: () =>
    apiFetch<{ connected: boolean; account_email: string | null; status: string | null }>(
      "/api/v1/integrations/microsoft-teams/connection",
    ),
  teamsConnect: () =>
    apiFetch<{ authorization_url: string }>("/api/v1/integrations/microsoft-teams/connect", {
      method: "POST",
    }),
  googleMeetConnection: () =>
    apiFetch<{ connected: boolean; account_email: string | null; status: string | null }>(
      "/api/v1/integrations/google-meet/connection",
    ),
  googleMeetConnect: () =>
    apiFetch<{ authorization_url: string }>("/api/v1/integrations/google-meet/connect", { method: "POST" }),
  googleMeetDisconnect: () => apiFetch<void>("/api/v1/integrations/google-meet/connection", { method: "DELETE" }),
  teamsDisconnect: () =>
    apiFetch<void>("/api/v1/integrations/microsoft-teams/connection", { method: "DELETE" }),
  teamsAdminConsentUrl: () =>
    apiFetch<{ url: string }>("/api/v1/integrations/microsoft-teams/admin-consent-url"),
  teamsChats: () =>
    apiFetch<{ id: string; topic: string | null; chat_type: string }[]>(
      "/api/v1/integrations/microsoft-teams/chats",
    ),
  teamsLinkChat: (chat_id: string, contact_id: string) =>
    apiFetch<{ session_id: string }>("/api/v1/integrations/microsoft-teams/chats/link", {
      method: "POST",
      body: { chat_id, contact_id },
    }),

  simulateMessage: (
    kind: "whatsapp-message" | "teams-message",
    body: { text: string; contact_id?: string; phone?: string; chat_id?: string; sender_name?: string },
  ) =>
    apiFetch<Record<string, number | string>>(`/api/v1/integrations/dev/simulate/${kind}`, {
      method: "POST",
      body,
    }),
};

export const STATE_STYLE: Record<CapabilityState, string> = {
  NOT_CONFIGURED: "bg-slate-100 text-slate-600",
  CONFIGURED: "bg-sky-100 text-sky-800",
  VALIDATED: "bg-indigo-100 text-indigo-800",
  ENABLED: "bg-emerald-100 text-emerald-800",
  ERROR: "bg-rose-100 text-rose-800",
  NOT_AVAILABLE: "bg-slate-100 text-slate-400",
};

/** Build the PUT body: only changed non-secret fields and newly typed secrets are sent.
 * An empty secret input keeps the stored secret. */
export function buildConfigureBody(
  fields: IntegrationField[],
  form: Record<string, string>,
  mode: "LIVE" | "MOCK",
): ConfigureInput {
  const values: Record<string, string | null> = {};
  for (const f of fields) {
    const typed = (form[f.key] ?? "").trim();
    if (f.kind === "secret") {
      if (typed) values[f.key] = typed;
    } else if (typed !== (f.value ?? "")) {
      values[f.key] = typed || null;
    }
  }
  return { mode, values };
}
