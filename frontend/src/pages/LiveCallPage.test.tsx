import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Insight, LiveState } from "../lib/live";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const hook = vi.hoisted(() => ({ useLiveCall: vi.fn() }));
vi.mock("../lib/useLiveCall", () => hook);
const api = vi.hoisted(() => ({ simulate: vi.fn(), end: vi.fn(), start: vi.fn(), dismiss: vi.fn(), addNote: vi.fn(), reviewNote: vi.fn(), deleteNote: vi.fn(), snapshot: vi.fn() }));
vi.mock("../lib/live", async (orig) => ({ ...(await orig<typeof import("../lib/live")>()), liveApi: api }));
const { LiveCallPage } = await import("./LiveCallPage");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });
const card = (id: string, type: Insight["type"], priority: Insight["priority"], content: string): Insight => ({
  id, type, priority, content, context: null, confidence: 0.6, source: "DETERMINISTIC", status: "ACTIVE", created_at: `2026-01-01T00:00:0${id}Z`,
});

function state(overrides: Partial<LiveState> = {}): LiveState {
  return {
    epoch: "e", seq: 1, partial: null, lastTranscriptAt: null, simulation_available: true,
    pipeline: { session_active: true, stt: "ok", copilot: "ok" },
    call: {
      id: "c1", contact_id: "k1", contact: { id: "k1", name: "Ravi Kumar", organization: null }, user_id: "u1",
      objective: "Qualify need", desired_outcome: null, status: "ACTIVE", scheduled_at: null,
      started_at: new Date(Date.now() - 65_000).toISOString(), ended_at: null, duration_seconds: null,
      outcome: null, outcome_notes: null, next_step: null, created_at: "x", updated_at: "x",
    },
    agenda: [], transcript: [
      { id: "s1", seq: 1, speaker: "CUSTOMER", text: "We have 5 branches", start_ms: 0, end_ms: 1, stt_confidence: 0.9, speaker_confidence: 1, source: "mock", is_final: true },
    ],
    insights: [
      card("1", "IMPORTANT_FACT", "LOW", "Budget: 2 lakh"),
      card("2", "OBJECTION", "HIGH", "Objection (price): too expensive"),
      card("3", "QUESTION_SUGGESTION", "MEDIUM", "How many locations?"),
      card("4", "NEXT_STEP", "MEDIUM", "Send quotation"),
    ],
    notes: [],
    ...overrides,
  };
}

beforeEach(() => {
  hook.useLiveCall.mockReset();
  Object.values(api).forEach((m) => m.mockReset());
});

describe("LiveCallPage", () => {
  it("shows the call, transcript and at most three prioritised cards", () => {
    hook.useLiveCall.mockReturnValue({ state: state(), error: null, connection: "live", reload: vi.fn(), patch: vi.fn() });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByText("Ravi Kumar")).toBeInTheDocument();
    expect(screen.getByLabelText("Call duration").textContent).toMatch(/^01:0\d$/);
    expect(screen.getByText("We have 5 branches")).toBeInTheDocument();
    const copilot = within(screen.getByLabelText("AI copilot"));
    expect(copilot.getAllByRole("article")).toHaveLength(3);
    expect(copilot.getAllByRole("article")[0]).toHaveTextContent("too expensive");
    expect(copilot.queryByText("Budget: 2 lakh")).not.toBeInTheDocument();
    expect(copilot.getByText("Show all (4)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End call" })).toBeInTheDocument();
  });

  it("explains degraded transcription without implying the call dropped", () => {
    hook.useLiveCall.mockReturnValue({
      state: state({ pipeline: { session_active: true, stt: "unavailable", copilot: "degraded" } }),
      error: null, connection: "reconnecting", reload: vi.fn(), patch: vi.fn(),
    });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByRole("status")).toHaveTextContent("Your phone call continues normally");
    expect(screen.getByText("○ Reconnecting…")).toBeInTheDocument();
  });

  it("offers the mock simulation for an initiated call", async () => {
    const reload = vi.fn().mockResolvedValue(undefined);
    const s = state();
    hook.useLiveCall.mockReturnValue({ state: { ...s, call: { ...s.call, status: "INITIATED" } }, error: null, connection: "live", reload, patch: vi.fn() });
    api.simulate.mockResolvedValue({ status: "started" });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    await userEvent.click(screen.getByRole("button", { name: "Simulate conversation (mock)" }));
    await waitFor(() => expect(api.simulate).toHaveBeenCalledWith("c1"));
    expect(reload).toHaveBeenCalled();
  });

  it("identifies a Teams call and explains transient (not stored) mode", () => {
    const s = state();
    hook.useLiveCall.mockReturnValue({
      state: { ...s, channel: "TEAMS", transcript_persistence: "TRANSIENT", call: { ...s.call, channel: "TEAMS", transcript_persistence: "TRANSIENT" } },
      error: null, connection: "live", reload: vi.fn(), patch: vi.fn(),
    });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByText(/Channel: Microsoft Teams/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("not stored after the call");
  });

  it("labels phone calls", () => {
    hook.useLiveCall.mockReturnValue({ state: state(), error: null, connection: "live", reload: vi.fn(), patch: vi.fn() });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByText(/Channel: Phone/)).toBeInTheDocument();
  });

  it("shows Teams media unavailable with the compliance reason", () => {
    const s = state();
    hook.useLiveCall.mockReturnValue({
      state: { ...s, channel: "TEAMS", pipeline: { ...s.pipeline, media: "unavailable", media_reason: "recording_status_failed" } },
      error: null, connection: "live", reload: vi.fn(), patch: vi.fn(),
    });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByRole("status")).toHaveTextContent("Teams meeting audio is not reaching the copilot");
    expect(screen.getByRole("status")).toHaveTextContent("did not confirm the recording status");
    expect(screen.getByRole("status")).toHaveTextContent("The meeting itself continues");
  });

  it("shows connecting, copilot processing and transcript receiving states", () => {
    const s = state();
    hook.useLiveCall.mockReturnValue({
      state: { ...s, lastTranscriptAt: Date.now(), pipeline: { ...s.pipeline, copilot_processing: true } },
      error: null, connection: "connecting", reload: vi.fn(), patch: vi.fn(),
    });
    renderWithAuth(<LiveCallPage />, { auth, path: "/calls/:id/live", url: "/calls/c1/live" });
    expect(screen.getByText("○ Connecting…")).toBeInTheDocument();
    expect(screen.getByText("analysing…")).toBeInTheDocument();
    expect(screen.getByText("● Receiving transcript")).toBeInTheDocument();
  });
});
