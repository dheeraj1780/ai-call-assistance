import { apiFetch } from "./api";
import { qs } from "./crm";

export interface CalendarConnection {
  connected: boolean;
  provider: string | null;
  account_email: string | null;
  status: "ACTIVE" | "REAUTH_REQUIRED" | null;
}

export interface ExternalEvent {
  external_id: string;
  title: string;
  starts_at: string;
  ends_at: string;
  html_link: string | null;
}

export interface AppCalendarEvent {
  id: string;
  provider: string;
  external_event_id: string;
  title: string;
  starts_at: string;
  ends_at: string;
  html_link: string | null;
  contact_id: string | null;
  call_id: string | null;
  action_item_id: string | null;
}

export interface NewCalendarEvent {
  title: string;
  starts_at: string;
  ends_at: string;
  description?: string;
  contact_id?: string;
  call_id?: string;
  action_item_id?: string;
}

export const calendarApi = {
  connection: () => apiFetch<CalendarConnection>("/api/v1/calendar/connection"),
  connect: () => apiFetch<{ authorization_url: string }>("/api/v1/calendar/connect", { method: "POST" }),
  disconnect: () => apiFetch<void>("/api/v1/calendar/connection", { method: "DELETE" }),
  upcoming: (days = 14) => apiFetch<ExternalEvent[]>(`/api/v1/calendar/upcoming${qs({ days })}`),
  appEvents: (contactId?: string) =>
    apiFetch<AppCalendarEvent[]>(`/api/v1/calendar/events${qs({ contact_id: contactId })}`),
  // confirm: true is required by the API - only sent from an explicit user confirmation.
  create: (event: NewCalendarEvent) =>
    apiFetch<AppCalendarEvent>("/api/v1/calendar/events", { method: "POST", body: { ...event, confirm: true } }),
};
