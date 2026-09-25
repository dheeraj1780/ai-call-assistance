import { describe, expect, it } from "vitest";

import {
  TRANSCRIPT_DELAY_S,
  applyEvent,
  finalisingMessage,
  fromSnapshot,
  transcriptHealth,
  visibleInsights,
  type Insight,
  type LiveSnapshot,
  type LiveState,
} from "./live";

const snap: LiveSnapshot = {
  epoch: "e1",
  seq: 5,
  call: {
    id: "c1",
    contact_id: "k1",
    contact: { id: "k1", name: "Ravi", organization: null },
    user_id: "u1",
    objective: null,
    desired_outcome: null,
    status: "INITIATED",
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_seconds: null,
    outcome: null,
    outcome_notes: null,
    next_step: null,
    created_at: "x",
    updated_at: "x",
  },
  agenda: [
    {
      id: "a1",
      call_id: "c1",
      position: 0,
      title: "Budget",
      question: null,
      description: null,
      source: "MANUAL",
      status: "NOT_STARTED",
      status_source: "DEFAULT",
      status_confidence: null,
      status_reason: null,
      completed_at: null,
    },
  ],
  transcript: [],
  insights: [],
  notes: [],
  pipeline: { session_active: true, stt: "ok", copilot: "ok" },
  simulation_available: true,
};

const seg = (seq: number) => ({
  id: `s${seq}`,
  seq,
  speaker: "CUSTOMER",
  text: `line ${seq}`,
  start_ms: 0,
  end_ms: 0,
  stt_confidence: null,
  speaker_confidence: 1,
  source: "mock",
  is_final: true,
});

describe("live reducer", () => {
  it("applies events in order and ignores replays", () => {
    let s = fromSnapshot(snap);
    s = applyEvent(s, { type: "call.status", seq: 6, epoch: "e1", data: { status: "ACTIVE", started_at: "t" } });
    expect(s.call.status).toBe("ACTIVE");
    s = applyEvent(s, { type: "transcript.partial", seq: 7, epoch: "e1", data: { speaker: "CUSTOMER", text: "we have" } });
    expect(s.partial?.text).toBe("we have");
    s = applyEvent(s, { type: "transcript.final", seq: 8, epoch: "e1", data: seg(1) });
    expect(s.partial).toBeNull();
    expect(s.transcript).toHaveLength(1);
    const replay = applyEvent(s, { type: "transcript.final", seq: 8, epoch: "e1", data: seg(2) });
    expect(replay).toBe(s); // seq already applied
    s = applyEvent(s, { type: "agenda.updated", seq: 9, epoch: "e1", data: { id: "a1", status: "COMPLETED" } });
    expect(s.agenda[0]!.status).toBe("COMPLETED");
    s = applyEvent(s, { type: "stt.status", seq: 10, epoch: "e1", data: { state: "unavailable" } });
    expect(s.pipeline.stt).toBe("unavailable");
    s = applyEvent(s, {
      type: "note.upserted",
      seq: 11,
      epoch: "e1",
      data: { id: "n1", kind: "OBJECTION", text: "too expensive", status: "SUGGESTED" },
    });
    expect(s.notes).toHaveLength(1);
    s = applyEvent(s, { type: "note.upserted", seq: 12, epoch: "e1", data: { id: "n1", kind: "OBJECTION", text: "x", status: "REJECTED" } });
    expect(s.notes).toHaveLength(0);
  });

  it("shows at most three cards, highest priority first", () => {
    const mk = (id: string, priority: Insight["priority"], created: string): Insight => ({
      id,
      type: "IMPORTANT_FACT",
      priority,
      content: id,
      context: null,
      confidence: null,
      source: "DETERMINISTIC",
      status: "ACTIVE",
      created_at: created,
    });
    const cards = visibleInsights([
      mk("low", "LOW", "2026-01-01T00:00:05Z"),
      mk("high-old", "HIGH", "2026-01-01T00:00:01Z"),
      mk("med", "MEDIUM", "2026-01-01T00:00:03Z"),
      mk("high-new", "HIGH", "2026-01-01T00:00:04Z"),
    ]);
    expect(cards.map((c) => c.id)).toEqual(["high-new", "high-old", "med"]);
  });
});

describe("channel/media events", () => {
  const base = (): LiveState => ({
    ...fromSnapshot({
      epoch: "e", seq: 0, agenda: [], transcript: [], insights: [], notes: [], simulation_available: false,
      pipeline: { session_active: true, stt: "ok", copilot: "ok" },
      call: { id: "c", contact_id: "k", contact: { id: "k", name: "n", organization: null }, user_id: null, objective: null,
        desired_outcome: null, status: "ACTIVE", scheduled_at: null, started_at: null, ended_at: null, duration_seconds: null,
        outcome: null, outcome_notes: null, next_step: null, created_at: "x", updated_at: "x" },
    }),
  });

  it("tracks media status, copilot processing and transcript timing", () => {
    let s = applyEvent(base(), { type: "media.status", seq: 1, epoch: "e", data: { state: "unavailable", reason: "media_socket_connect_failed" } });
    expect(s.pipeline.media).toBe("unavailable");
    expect(s.pipeline.media_reason).toBe("media_socket_connect_failed");
    s = applyEvent(s, { type: "copilot.processing", seq: 2, epoch: "e", data: { state: "started" } });
    expect(s.pipeline.copilot_processing).toBe(true);
    s = applyEvent(s, { type: "copilot.processing", seq: 3, epoch: "e", data: { state: "done", ms: 900 } });
    expect(s.pipeline.copilot_processing).toBe(false);
    expect(s.lastTranscriptAt).toBeNull();
    s = applyEvent(s, { type: "transcript.partial", seq: 4, epoch: "e", data: { speaker: "CUSTOMER", text: "we" } });
    expect(s.lastTranscriptAt).not.toBeNull();
  });

  it("shows end-of-call finalising phases until the session closes", () => {
    let s = base();
    expect(finalisingMessage(s)).toBeNull();
    s = applyEvent(s, { type: "session.phase", seq: 1, epoch: "e", data: { phase: "input_finished" } });
    expect(finalisingMessage(s)).toBe("Finalising transcript…");
    s = applyEvent(s, { type: "session.phase", seq: 2, epoch: "e", data: { phase: "stt_drained", complete: true } });
    expect(finalisingMessage(s)).toBe("Finishing AI notes…");
    s = applyEvent(s, { type: "session.phase", seq: 3, epoch: "e", data: { phase: "closed" } });
    expect(finalisingMessage(s)).toBeNull();
  });

  it("reports transcript health without implying the call dropped", () => {
    const s = base();
    const media = { ...s, pipeline: { ...s.pipeline, media: "available" as const } };
    expect(transcriptHealth(media, Date.now())).toBe("waiting");
    expect(transcriptHealth({ ...media, lastTranscriptAt: 1_000 }, 1_000 + 5_000)).toBe("receiving");
    expect(transcriptHealth({ ...media, lastTranscriptAt: 1_000 }, 1_000 + (TRANSCRIPT_DELAY_S + 1) * 1000)).toBe("delayed");
    expect(transcriptHealth({ ...media, pipeline: { ...media.pipeline, stt: "unavailable" } }, 0)).toBeNull();
    expect(transcriptHealth({ ...media, call: { ...media.call, status: "COMPLETED" } }, 0)).toBeNull();
  });
});
