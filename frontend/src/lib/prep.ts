import { apiFetch } from "./api";
import type { ActionItem, Call, Contact, ContactNote, TimelineEvent } from "./crm";

export type AgendaStatus = "NOT_STARTED" | "IN_PROGRESS" | "COMPLETED" | "SKIPPED";
export type AgendaSource = "MANUAL" | "AI" | "AI_EDITED";

export interface AgendaItem {
  id: string;
  call_id: string;
  position: number;
  title: string;
  question: string | null;
  description: string | null;
  source: AgendaSource;
  status: AgendaStatus;
  status_source: "DEFAULT" | "DETERMINISTIC" | "AI" | "MANUAL";
  status_confidence: number | null;
  status_reason: string | null;
  completed_at: string | null;
}

export interface AgendaDraftItem {
  key: string;
  title: string;
  question: string;
  source: AgendaSource;
}

export interface KnownItem {
  kind: string;
  text: string;
  status: string;
  call_id: string | null;
}

export interface CallPrep {
  call: Call;
  contact: Contact;
  agenda: AgendaItem[];
  previous_calls: {
    id: string;
    status: string;
    started_at: string | null;
    ended_at: string | null;
    objective: string | null;
    outcome: string | null;
    next_step: string | null;
    summary: string | null;
  }[];
  recent_notes: ContactNote[];
  open_action_items: ActionItem[];
  known_requirements: KnownItem[];
  known_objections: KnownItem[];
  recent_timeline: TimelineEvent[];
}

export interface AgendaSuggestion {
  customer_facts: { text: string; source_ref: string }[];
  ai_suggested_agenda: { title: string; question: string | null; why: string | null }[];
  ai_open_questions: string[];
  provider: string;
  discarded_ungrounded_facts: number;
}

export const prepApi = {
  prep: (callId: string) => apiFetch<CallPrep>(`/api/v1/calls/${callId}/prep`),
  saveAgenda: (callId: string, items: AgendaDraftItem[]) =>
    apiFetch<AgendaItem[]>(`/api/v1/calls/${callId}/agenda`, {
      method: "PUT",
      body: {
        items: items.map((i) => ({
          title: i.title.trim(),
          question: i.question.trim() || null,
          source: i.source,
        })),
      },
    }),
  suggest: (callId: string) =>
    apiFetch<AgendaSuggestion>(`/api/v1/calls/${callId}/agenda/suggest`, { method: "POST" }),
  setItemStatus: (callId: string, itemId: string, status: AgendaStatus) =>
    apiFetch<AgendaItem>(`/api/v1/calls/${callId}/agenda/${itemId}`, { method: "PATCH", body: { status } }),
};

let counter = 0;
export function draftKey(): string {
  counter += 1;
  return `d${counter}`;
}
