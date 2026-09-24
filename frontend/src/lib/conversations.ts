/** Conversations (WhatsApp / Teams messages) and per-contact communication options. */
import { apiFetch } from "./api";
import { qs } from "./crm";
import type { CallNote } from "./live";

export interface ConversationSession {
  id: string;
  provider: string;
  channel: "WHATSAPP" | "TEAMS" | "PHONE";
  capability: string;
  status: string;
  match_status: "MATCHED" | "UNMATCHED" | "AMBIGUOUS";
  contact: { id: string; name: string; organization: string | null } | null;
  call_id: string | null;
  display_name: string | null;
  external_participant_id: string | null;
  last_message_at: string | null;
  last_inbound_at: string | null;
  mock: boolean;
  preview: string | null;
}

export interface Message {
  id: string;
  direction: "INBOUND" | "OUTBOUND";
  sender_type: "CUSTOMER" | "SALESPERSON" | "BOT" | "SYSTEM";
  sender_user_id: string | null;
  message_type: string;
  content: string;
  status: "RECEIVED" | "SENDING" | "SENT" | "DELIVERED" | "READ" | "FAILED";
  error_code: string | null;
  occurred_at: string;
}

export interface Draft {
  id: string;
  reply_to_message_id: string | null;
  body: string;
  source: "AI" | "MANUAL";
  status: "SUGGESTED" | "SENT" | "DISCARDED";
  warnings: string[];
  ai_provider: string | null;
  created_at: string;
}

export interface Conversation {
  session: ConversationSession;
  messages: Message[];
  drafts: Draft[];
  notes: CallNote[];
  can_send: boolean;
  send_blocked_reason: string | null;
}

export interface ChannelAction {
  key: string;
  channel: string;
  capability: string;
  label: string;
  available: boolean;
  reason: string | null;
  mock: boolean;
  provider: string | null;
}

export const CHANNEL_LABEL: Record<string, string> = {
  WHATSAPP: "WhatsApp",
  TEAMS: "Microsoft Teams",
  PHONE: "Phone",
};

export const conversationsApi = {
  list: (params: { contact_id?: string; match_status?: string; channel?: string } = {}) =>
    apiFetch<ConversationSession[]>(`/api/v1/conversations${qs(params)}`),
  get: (id: string) => apiFetch<Conversation>(`/api/v1/conversations/${id}`),
  send: (id: string, body: { text: string; client_message_id: string; draft_id?: string }) =>
    apiFetch<Message>(`/api/v1/conversations/${id}/messages`, { method: "POST", body }),
  updateDraft: (id: string, draftId: string, body: { status?: "DISCARDED"; body?: string }) =>
    apiFetch<Draft>(`/api/v1/conversations/${id}/drafts/${draftId}`, { method: "PATCH", body }),
  link: (id: string, contact_id: string) =>
    apiFetch<ConversationSession>(`/api/v1/conversations/${id}/link`, {
      method: "POST",
      body: { contact_id },
    }),
  open: (contactId: string, channel: "WHATSAPP" | "TEAMS") =>
    apiFetch<ConversationSession>(`/api/v1/contacts/${contactId}/conversations`, {
      method: "POST",
      body: { channel },
    }),
  options: (contactId: string) =>
    apiFetch<ChannelAction[]>(`/api/v1/contacts/${contactId}/communication`),
};

/** Idempotency key for one send attempt (a UUID; retries of the same click reuse it). */
export function newClientMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20)}`;
}
