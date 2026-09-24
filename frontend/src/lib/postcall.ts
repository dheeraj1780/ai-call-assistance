import { apiFetch } from "./api";
import type { Call } from "./crm";

export type FieldStatus = "CONFIRMED" | "INFERRED" | "NOT_DISCUSSED";

export interface GroundedField {
  value: string | null;
  status: FieldStatus;
}

export interface CallSummary {
  status: "PENDING" | "READY" | "FAILED";
  provider: string | null;
  error_code: string | null;
  summary: string | null;
  suggested_outcome: string | null;
  current_solution: GroundedField;
  budget: GroundedField;
  timeline: GroundedField;
  decision_maker: GroundedField;
  next_step: GroundedField;
  generated_at: string | null;
}

export interface FollowUpDraft {
  id: string;
  channel: "EMAIL" | "WHATSAPP" | "GENERAL";
  subject: string | null;
  body: string;
  status: "DRAFT" | "APPROVED" | "COPIED" | "DISCARDED";
  source: "AI" | "MANUAL";
  warnings: string[];
  reviewed_at: string | null;
}

export interface PostCall {
  call: Call;
  summary: CallSummary | null;
  drafts: FollowUpDraft[];
  auto_send: false;
}

export const postCallApi = {
  get: (callId: string) => apiFetch<PostCall>(`/api/v1/calls/${callId}/post-call`),
  retry: (callId: string) => apiFetch<void>(`/api/v1/calls/${callId}/post-call/retry`, { method: "POST" }),
  reviewDraft: (
    callId: string,
    draftId: string,
    body: { subject?: string; body?: string; status?: FollowUpDraft["status"] },
  ) => apiFetch<FollowUpDraft>(`/api/v1/calls/${callId}/follow-ups/${draftId}`, { method: "PATCH", body }),
};

export const FIELD_LABELS: [keyof Pick<CallSummary, "current_solution" | "budget" | "timeline" | "decision_maker" | "next_step">, string][] = [
  ["current_solution", "Current solution"],
  ["budget", "Budget"],
  ["timeline", "Timeline"],
  ["decision_maker", "Decision maker"],
  ["next_step", "Next step"],
];
