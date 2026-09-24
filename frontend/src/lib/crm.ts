/** CRM types and API calls (contacts, notes, timeline, calls, action items, members). */
import { apiFetch } from "./api";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export const CONTACT_STATUSES = [
  "NEW",
  "CONTACTED",
  "QUALIFIED",
  "PROPOSAL",
  "NEGOTIATION",
  "WON",
  "LOST",
] as const;
export type ContactStatus = (typeof CONTACT_STATUSES)[number];

export const CONTACT_SOURCES = [
  "MANUAL",
  "REFERRAL",
  "WEBSITE",
  "INBOUND_CALL",
  "EVENT",
  "IMPORT",
  "OTHER",
] as const;
export type ContactSource = (typeof CONTACT_SOURCES)[number];

export interface Contact {
  id: string;
  name: string;
  organization: string | null;
  phone: string | null;
  email: string | null;
  designation: string | null;
  status: ContactStatus;
  source: ContactSource;
  tags: string[];
  attributes: Record<string, string>;
  notes: string | null;
  owner_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export type ContactInput = Partial<
  Pick<
    Contact,
    | "name"
    | "organization"
    | "phone"
    | "email"
    | "designation"
    | "status"
    | "source"
    | "tags"
    | "notes"
    | "owner_user_id"
  >
>;

export interface ContactNote {
  id: string;
  contact_id: string;
  author_user_id: string | null;
  body: string;
  created_at: string;
  updated_at: string;
}

export const TIMELINE_CATEGORIES = [
  "CALL",
  "NOTE",
  "TASK",
  "FOLLOW_UP",
  "APPOINTMENT",
  "STATUS_CHANGE",
  "CONTACT",
] as const;
export type TimelineCategory = (typeof TIMELINE_CATEGORIES)[number];

export interface TimelineEvent {
  id: string;
  contact_id: string;
  category: TimelineCategory | string;
  event_type: string;
  occurred_at: string;
  actor_user_id: string | null;
  summary: string;
  from_status: string | null;
  to_status: string | null;
  note_id: string | null;
  call_id: string | null;
  action_item_id: string | null;
}

export interface TimelinePage {
  items: TimelineEvent[];
  next_cursor: string | null;
}

export type CallStatus =
  | "PLANNED"
  | "INITIATED"
  | "RINGING"
  | "CONNECTED"
  | "ACTIVE"
  | "COMPLETED"
  | "NO_ANSWER"
  | "CANCELLED"
  | "FAILED";

export const CALL_OUTCOMES = [
  "INTERESTED",
  "NOT_INTERESTED",
  "FOLLOW_UP_REQUIRED",
  "MEETING_BOOKED",
  "WON",
  "LOST",
  "NO_DECISION",
] as const;
export type CallOutcome = (typeof CALL_OUTCOMES)[number];

export interface Call {
  id: string;
  contact_id: string;
  contact: { id: string; name: string; organization: string | null };
  user_id: string | null;
  objective: string | null;
  desired_outcome: string | null;
  status: CallStatus;
  scheduled_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  outcome: CallOutcome | null;
  outcome_notes: string | null;
  next_step: string | null;
  created_at: string;
  updated_at: string;
}

export type ActionItemKind = "TASK" | "FOLLOW_UP" | "APPOINTMENT";
export type ActionItemStatus = "OPEN" | "IN_PROGRESS" | "DONE" | "CANCELLED";

export interface ActionItem {
  id: string;
  title: string;
  description: string | null;
  kind: ActionItemKind;
  status: ActionItemStatus;
  source: "MANUAL" | "AI";
  contact_id: string | null;
  contact: { id: string; name: string; organization: string | null } | null;
  call_id: string | null;
  assignee_user_id: string | null;
  created_by_user_id: string | null;
  due_at: string | null;
  confirmed_at: string | null;
  is_confirmed: boolean;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Member {
  user_id: string;
  email: string;
  full_name: string;
  role: string;
}

type Query = Record<string, string | number | boolean | string[] | null | undefined>;

export function qs(params: Query): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

export const crm = {
  listContacts: (params: Query) => apiFetch<Page<Contact>>(`/api/v1/contacts${qs(params)}`),
  getContact: (id: string) => apiFetch<Contact>(`/api/v1/contacts/${id}`),
  createContact: (body: ContactInput) =>
    apiFetch<Contact>("/api/v1/contacts", { method: "POST", body }),
  updateContact: (id: string, body: ContactInput) =>
    apiFetch<Contact>(`/api/v1/contacts/${id}`, { method: "PATCH", body }),
  deleteContact: (id: string) => apiFetch<void>(`/api/v1/contacts/${id}`, { method: "DELETE" }),

  timeline: (id: string, params: Query) =>
    apiFetch<TimelinePage>(`/api/v1/contacts/${id}/timeline${qs(params)}`),

  listNotes: (id: string) => apiFetch<Page<ContactNote>>(`/api/v1/contacts/${id}/notes?limit=100`),
  addNote: (id: string, body: string) =>
    apiFetch<ContactNote>(`/api/v1/contacts/${id}/notes`, { method: "POST", body: { body } }),
  deleteNote: (contactId: string, noteId: string) =>
    apiFetch<void>(`/api/v1/contacts/${contactId}/notes/${noteId}`, { method: "DELETE" }),

  listCalls: (params: Query) => apiFetch<Page<Call>>(`/api/v1/calls${qs(params)}`),
  getCall: (id: string) => apiFetch<Call>(`/api/v1/calls/${id}`),
  createCall: (body: {
    contact_id: string;
    objective?: string;
    desired_outcome?: string;
    scheduled_at?: string;
  }) => apiFetch<Call>("/api/v1/calls", { method: "POST", body }),
  updateCall: (id: string, body: Partial<Call>) =>
    apiFetch<Call>(`/api/v1/calls/${id}`, { method: "PATCH", body }),

  listActionItems: (params: Query) =>
    apiFetch<Page<ActionItem>>(`/api/v1/action-items${qs(params)}`),
  createActionItem: (body: {
    title: string;
    kind?: ActionItemKind;
    contact_id?: string;
    call_id?: string;
    due_at?: string;
    assignee_user_id?: string | null;
    description?: string;
  }) => apiFetch<ActionItem>("/api/v1/action-items", { method: "POST", body }),
  updateActionItem: (id: string, body: Partial<ActionItem>) =>
    apiFetch<ActionItem>(`/api/v1/action-items/${id}`, { method: "PATCH", body }),
  confirmActionItem: (id: string) =>
    apiFetch<ActionItem>(`/api/v1/action-items/${id}/confirm`, { method: "POST" }),
  deleteActionItem: (id: string) =>
    apiFetch<void>(`/api/v1/action-items/${id}`, { method: "DELETE" }),

  listMembers: () => apiFetch<Page<Member>>("/api/v1/companies/current/members?limit=200"),
};

export function label(value: string): string {
  const s = value.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Convert a <input type="datetime-local"> value to an ISO string with the local offset. */
export function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return undefined;
  return d.toISOString();
}
