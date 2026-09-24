/** Live call state: snapshot + WebSocket events (pure reducer, easy to test). */
import { apiFetch } from "./api";
import type { Call } from "./crm";
import type { AgendaItem, AgendaStatus } from "./prep";

export interface Segment {
  id: string;
  seq: number;
  speaker: "SALES_REP" | "CUSTOMER" | "UNKNOWN";
  text: string;
  start_ms: number;
  end_ms: number;
  stt_confidence: number | null;
  speaker_confidence: number | null;
  source: string;
  is_final: boolean;
}

export type InsightType =
  | "QUESTION_SUGGESTION"
  | "MISSING_AGENDA_ITEM"
  | "CUSTOMER_REQUIREMENT"
  | "OBJECTION"
  | "IMPORTANT_FACT"
  | "NEXT_STEP"
  | "KNOWLEDGE_RESULT";

export interface Insight {
  id: string;
  type: InsightType;
  priority: "LOW" | "MEDIUM" | "HIGH";
  content: string;
  context: string | null;
  confidence: number | null;
  source: "DETERMINISTIC" | "AI" | "KNOWLEDGE" | "MANUAL";
  status: "ACTIVE" | "DISMISSED";
  created_at: string | null;
}

export const NOTE_KINDS = [
  "REQUIREMENT",
  "PAIN_POINT",
  "CURRENT_SOLUTION",
  "BUDGET",
  "TIMELINE",
  "DECISION_MAKER",
  "COMPETITOR",
  "OBJECTION",
  "PREFERENCE",
  "NEXT_STEP",
  "IMPORTANT_FACT",
  "GENERAL",
] as const;
export type NoteKind = (typeof NOTE_KINDS)[number];

export interface CallNote {
  id: string;
  kind: NoteKind;
  category: string | null;
  text: string;
  confidence: number | null;
  source: "MANUAL" | "DETERMINISTIC" | "AI" | "KNOWLEDGE";
  status: "SUGGESTED" | "CONFIRMED" | "EDITED" | "REJECTED";
  created_at: string | null;
  updated_at: string | null;
}

export interface LiveSnapshot {
  epoch: string;
  seq: number;
  call: Call;
  agenda: AgendaItem[];
  transcript: Segment[];
  insights: Insight[];
  notes: CallNote[];
  pipeline: {
    session_active: boolean;
    stt: "ok" | "unavailable";
    copilot: "ok" | "degraded";
    /** Call audio reaching the API (Teams media gateway / provider stream). */
    media?: "available" | "unavailable" | "unknown";
    media_reason?: string | null;
    copilot_processing?: boolean;
  };
  simulation_available: boolean;
  channel?: "PHONE" | "TEAMS";
  transcript_persistence?: "PERSISTED" | "TRANSIENT" | "PENDING_RECORDING_STATUS";
}

export interface LiveState extends LiveSnapshot {
  partial: { speaker: string; text: string } | null;
  /** Browser time (ms) of the last transcript event, to show "receiving" vs "delayed". */
  lastTranscriptAt: number | null;
}

export interface LiveMessage {
  type: string;
  seq?: number;
  epoch?: string;
  data?: Record<string, unknown>;
}

export function fromSnapshot(s: LiveSnapshot): LiveState {
  return { ...s, partial: null, lastTranscriptAt: null };
}

/** Apply one server event. Unknown or stale (seq <= current) events are ignored. */
export function applyEvent(state: LiveState, msg: LiveMessage): LiveState {
  if (msg.seq !== undefined && msg.epoch === state.epoch && msg.seq <= state.seq) return state;
  const next: LiveState = { ...state, seq: msg.seq ?? state.seq, epoch: msg.epoch ?? state.epoch };
  const d = (msg.data ?? {}) as Record<string, unknown>;
  switch (msg.type) {
    case "call.status":
      return {
        ...next,
        call: {
          ...next.call,
          status: d.status as Call["status"],
          started_at: (d.started_at as string | null | undefined) ?? next.call.started_at,
          ended_at: (d.ended_at as string | null | undefined) ?? next.call.ended_at,
        },
      };
    case "transcript.partial":
      return { ...next, lastTranscriptAt: Date.now(), partial: { speaker: String(d.speaker), text: String(d.text) } };
    case "transcript.final": {
      const seg = d as unknown as Segment;
      if (next.transcript.some((s) => s.id === seg.id)) return next;
      return {
        ...next,
        lastTranscriptAt: Date.now(),
        partial: null,
        transcript: [...next.transcript, seg].sort((a, b) => a.seq - b.seq),
      };
    }
    case "agenda.updated":
      return {
        ...next,
        agenda: next.agenda.map((a) =>
          a.id === d.id
            ? {
                ...a,
                status: d.status as AgendaStatus,
                status_source: (d.status_source as AgendaItem["status_source"]) ?? a.status_source,
                status_reason: (d.status_reason as string | null) ?? a.status_reason,
              }
            : a,
        ),
      };
    case "insight.created": {
      const ins = d as unknown as Insight;
      if (next.insights.some((i) => i.id === ins.id)) return next;
      return { ...next, insights: [...next.insights, ins] };
    }
    case "insight.dismissed":
      return { ...next, insights: next.insights.filter((i) => i.id !== d.id) };
    case "note.upserted": {
      const note = d as unknown as CallNote;
      const others = next.notes.filter((n) => n.id !== note.id);
      return { ...next, notes: note.status === "REJECTED" ? others : [...others, note] };
    }
    case "note.deleted":
      return { ...next, notes: next.notes.filter((n) => n.id !== d.id) };
    case "stt.status":
      return { ...next, pipeline: { ...next.pipeline, stt: d.state === "ok" ? "ok" : "unavailable" } };
    case "copilot.status":
      return { ...next, pipeline: { ...next.pipeline, copilot: d.state === "ok" ? "ok" : "degraded" } };
    case "media.status":
      return {
        ...next,
        pipeline: {
          ...next.pipeline,
          media: d.state === "available" ? "available" : "unavailable",
          media_reason: (d.reason as string | null | undefined) ?? null,
        },
      };
    case "copilot.processing":
      return { ...next, pipeline: { ...next.pipeline, copilot_processing: d.state === "started" } };
    default:
      return next;
  }
}

const PRIORITY: Record<Insight["priority"], number> = { HIGH: 3, MEDIUM: 2, LOW: 1 };

/** The few cards worth showing while the salesperson is talking: newest, highest priority. */
export function visibleInsights(insights: Insight[], limit = 3): Insight[] {
  return [...insights]
    .filter((i) => i.status === "ACTIVE")
    .sort((a, b) => {
      const byPriority = PRIORITY[b.priority] - PRIORITY[a.priority];
      if (byPriority !== 0) return byPriority;
      return (b.created_at ?? "").localeCompare(a.created_at ?? "");
    })
    .slice(0, limit);
}

/** Seconds of silence from the transcript after which we say it may be delayed. */
export const TRANSCRIPT_DELAY_S = 20;

/** Live transcription health: receiving / waiting / delayed (never implies the call dropped). */
export function transcriptHealth(state: LiveState, now: number): "receiving" | "waiting" | "delayed" | null {
  if (state.call.status !== "ACTIVE" || state.pipeline.stt === "unavailable" || state.pipeline.media === "unavailable") return null;
  if (state.lastTranscriptAt === null) return "waiting";
  const age = (now - state.lastTranscriptAt) / 1000;
  return age < TRANSCRIPT_DELAY_S ? "receiving" : "delayed";
}

export const LIVE_STATUSES = new Set(["INITIATED", "RINGING", "CONNECTED", "ACTIVE"]);
export const TERMINAL_STATUSES = new Set(["COMPLETED", "NO_ANSWER", "CANCELLED", "FAILED"]);

export const liveApi = {
  snapshot: (callId: string) => apiFetch<LiveSnapshot>(`/api/v1/calls/${callId}/live`),
  start: (callId: string) => apiFetch<Call>(`/api/v1/calls/${callId}/start`, { method: "POST" }),
  end: (callId: string) => apiFetch<Call>(`/api/v1/calls/${callId}/end`, { method: "POST" }),
  simulate: (callId: string) => apiFetch<{ status: string }>(`/api/v1/calls/${callId}/simulate`, { method: "POST" }),
  dismiss: (callId: string, insightId: string) =>
    apiFetch<void>(`/api/v1/calls/${callId}/insights/${insightId}/dismiss`, { method: "POST" }),
  addNote: (callId: string, kind: NoteKind, text: string) =>
    apiFetch<CallNote>(`/api/v1/calls/${callId}/notes`, { method: "POST", body: { kind, text } }),
  reviewNote: (callId: string, noteId: string, body: { text?: string; status?: CallNote["status"] }) =>
    apiFetch<CallNote>(`/api/v1/calls/${callId}/notes/${noteId}`, { method: "PATCH", body }),
  deleteNote: (callId: string, noteId: string) =>
    apiFetch<void>(`/api/v1/calls/${callId}/notes/${noteId}`, { method: "DELETE" }),
};
