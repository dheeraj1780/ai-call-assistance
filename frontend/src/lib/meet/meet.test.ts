import { describe, expect, it, vi } from "vitest";

import type { MeetSessionStatus } from "../../vendor/meet-media-api/types/meetmediaapiclient";
import type { MeetStreamTrack } from "../../vendor/meet-media-api/types/mediatypes";
import type { Subscribable } from "../../vendor/meet-media-api/types/subscribable";
import { MeetConnectionState, MeetDisconnectReason } from "../../vendor/meet-media-api/types/enums";
import { MeetCopilotBridge, type BridgeDeps, type BridgeStatus, type SocketLike } from "./bridge";
import { parseMeetingCode } from "./meetingCode";
import { PcmFramer, SAMPLES_PER_FRAME, floatToPcm16, toBase64, toLittleEndianBytes } from "./pcm";

describe("parseMeetingCode", () => {
  it.each([
    ["https://meet.google.com/abc-defg-hij", "abc-defg-hij"],
    ["https://meet.google.com/ABC-DEFG-HIJ?authuser=1", "abc-defg-hij"],
    ["meet.google.com/abc-defg-hij", "abc-defg-hij"],
    ["abcdefghij", "abc-defg-hij"],
    ["http://meet.google.com/abc-defg-hij", null],
    ["https://meet.google.com.evil.com/abc-defg-hij", null],
    ["https://meet.google.com/lookup/xyz", null],
    ["not a link", null],
  ])("%s -> %s", (input, expected) => {
    expect(parseMeetingCode(input)).toBe(expected);
  });
});

describe("PCM normalisation", () => {
  it("converts floats to clipped 16-bit samples", () => {
    expect(Array.from(floatToPcm16(new Float32Array([0, 1, -1, 2, -2, 0.5])))).toEqual([
      0, 32767, -32768, 32767, -32768, 16384,
    ]);
  });

  it("emits exact 20 ms frames of 640 little-endian bytes and keeps the remainder", () => {
    const frames: Uint8Array[] = [];
    const framer = new PcmFramer((f) => frames.push(f));
    framer.push(new Int16Array(128)); // one Web Audio render quantum
    expect(frames).toHaveLength(0);
    framer.push(new Int16Array(SAMPLES_PER_FRAME * 2)); // 128 + 640 samples
    expect(frames).toHaveLength(2);
    expect(frames.every((f) => f.length === 640)).toBe(true);
    framer.reset();
    framer.push(new Int16Array(SAMPLES_PER_FRAME - 1));
    expect(frames).toHaveLength(2);
  });

  it("encodes little-endian and base64", () => {
    const bytes = toLittleEndianBytes(new Int16Array([1, -2]));
    expect(Array.from(bytes)).toEqual([1, 0, 254, 255]);
    expect(toBase64(bytes)).toBe("AQD+/w==");
  });
});

// ---- bridge with fakes (no Google, no WebRTC, no audio device) ----------------------------------

class FakeSubscribable<T> implements Subscribable<T> {
  private listeners: Array<(v: T) => void> = [];
  constructor(private value: T) {}
  get(): T {
    return this.value;
  }
  subscribe(cb: (v: T) => void): () => void {
    this.listeners.push(cb);
    return () => this.unsubscribe(cb);
  }
  unsubscribe(cb: (v: T) => void): boolean {
    const n = this.listeners.length;
    this.listeners = this.listeners.filter((l) => l !== cb);
    return this.listeners.length < n;
  }
  set(v: T): void {
    this.value = v;
    this.listeners.forEach((l) => l(v));
  }
}

function harness(connect?: BridgeDeps["connect"]) {
  const session = new FakeSubscribable<MeetSessionStatus>({ connectionState: MeetConnectionState.UNKNOWN });
  const tracks = new FakeSubscribable<MeetStreamTrack[]>([]);
  const sent: string[] = [];
  const socket: SocketLike = {
    readyState: 0,
    onopen: null,
    onclose: null,
    send: (d) => sent.push(d),
    close: vi.fn(),
  };
  let onSamples: (s: Float32Array) => void = () => undefined;
  const added: MediaStreamTrack[] = [];
  const sink = { addTrack: (t: MediaStreamTrack) => added.push(t), close: vi.fn(async () => undefined) };
  const events: Array<{ state: string; reason?: string }> = [];
  const client = {
    sessionStatus: session,
    meetStreamTracks: tracks,
    joinMeeting: vi.fn(async (protocol?: { connectActiveConference: (o: string) => Promise<{ answer: string }> }) => {
      await protocol?.connectActiveConference("v=0 offer");
    }),
    leaveMeeting: vi.fn(async () => {
      session.set({ connectionState: MeetConnectionState.DISCONNECTED, disconnectReason: MeetDisconnectReason.CLIENT_LEFT });
    }),
  };
  const deps: BridgeDeps = {
    createClient: vi.fn(() => client),
    createAudioSink: async (cb) => {
      onSamples = cb;
      return sink;
    },
    openSocket: vi.fn(() => socket),
    connect:
      connect ??
      vi.fn(async () => ({
        answer: "v=0 answer",
        trace_id: "t1",
        space: "spaces/S",
        media_ws_path: "/api/v1/telephony/media/google-meet/c1?token=tok",
        mock: false,
      })),
    reportEvent: vi.fn(async (_id, body) => {
      events.push({ state: body.state, ...(body.reason ? { reason: body.reason } : {}) });
    }),
    newId: () => `id-${Math.random().toString(36).slice(2, 12)}`,
  };
  const statuses: BridgeStatus[] = [];
  const bridge = new MeetCopilotBridge("c1", deps, (s) => statuses.push(s));
  const openSocket = () => {
    socket.readyState = 1;
    socket.onopen?.(new Event("open"));
  };
  return { bridge, deps, client, session, tracks, socket, sent, sink, added, events, statuses, openSocket, samples: (f: Float32Array) => onSamples(f) };
}

const audioTrack = (id: string) => ({ mediaStreamTrack: { kind: "audio", id } as MediaStreamTrack }) as MeetStreamTrack;

describe("MeetCopilotBridge", () => {
  it("signals via the API, streams mixed PCM to the media socket and leaves cleanly", async () => {
    const h = harness();
    await h.bridge.start();
    expect(h.deps.createClient).toHaveBeenCalledWith(
      expect.objectContaining({ numberOfVideoStreams: 0, enableAudioStreams: true, accessToken: "" }),
    );
    expect(h.deps.connect).toHaveBeenCalledWith("c1", "v=0 offer");
    expect(h.deps.openSocket).toHaveBeenCalledWith(expect.stringContaining("/api/v1/telephony/media/google-meet/c1?token=tok"));
    h.openSocket();
    expect(JSON.parse(h.sent[0]!)).toEqual({ event: "start", format: { encoding: "linear16", sample_rate: 16000 } });

    h.session.set({ connectionState: MeetConnectionState.WAITING });
    h.session.set({ connectionState: MeetConnectionState.JOINED });
    expect(h.bridge.current.phase).toBe("live");
    h.tracks.set([audioTrack("a1"), { mediaStreamTrack: { kind: "video", id: "v" } as MediaStreamTrack } as MeetStreamTrack]);
    expect(h.added).toHaveLength(1); // audio only: video tracks are ignored
    expect(h.bridge.current.audioTracks).toBe(1);

    h.samples(new Float32Array(640)); // 40 ms
    const media = h.sent.slice(1).map((m) => JSON.parse(m));
    expect(media).toHaveLength(2);
    expect(media[0]).toMatchObject({ event: "media", track: "mixed", seq: 1 });
    expect(atob(media[0].payload)).toHaveLength(640);

    await h.bridge.stop();
    expect(h.client.leaveMeeting).toHaveBeenCalled();
    expect(JSON.parse(h.sent.at(-1)!)).toEqual({ event: "stop" });
    expect(h.socket.close).toHaveBeenCalledWith(1000);
    expect(h.sink.close).toHaveBeenCalled();
    expect(h.bridge.current.phase).toBe("ended");
    expect(h.events).toEqual([
      { state: "waiting" },
      { state: "joined" },
      { state: "disconnected", reason: "CLIENT_LEFT" },
    ]);
  });

  it("attaches each new audio track once", async () => {
    const h = harness();
    await h.bridge.start();
    const a = audioTrack("a1");
    h.tracks.set([a]);
    h.tracks.set([a, audioTrack("a2")]);
    expect(h.added).toHaveLength(2);
    expect(h.bridge.current.audioTracks).toBe(2);
  });

  it("surfaces Developer Preview eligibility errors without faking a connection", async () => {
    const h = harness(
      vi.fn(async () => {
        throw Object.assign(new Error("Google refused access to live meeting media."), { code: "MEET_MEDIA_API_NOT_ELIGIBLE" });
      }),
    );
    h.client.joinMeeting.mockImplementation(async (p) => {
      await p?.connectActiveConference("v=0 offer");
    });
    await h.bridge.start();
    expect(h.bridge.current.phase).toBe("error");
    expect(h.bridge.current.error?.code).toBe("MEET_MEDIA_API_NOT_ELIGIBLE");
    expect(h.deps.openSocket).not.toHaveBeenCalled();
    expect(h.sink.close).toHaveBeenCalled();
  });

  it("refuses mock mode instead of pretending to receive audio", async () => {
    const h = harness(
      vi.fn(async () => ({ answer: "x", trace_id: "mock", space: "spaces/mock", media_ws_path: "/x", mock: true })),
    );
    await h.bridge.start();
    expect(h.bridge.current.error?.code).toBe("MEET_MOCK_MODE");
    expect(h.deps.openSocket).not.toHaveBeenCalled();
  });

  it("reports a failure when Google disconnects before the copilot was admitted", async () => {
    const h = harness();
    await h.bridge.start();
    h.session.set({ connectionState: MeetConnectionState.WAITING });
    h.session.set({ connectionState: MeetConnectionState.DISCONNECTED, disconnectReason: MeetDisconnectReason.SESSION_UNHEALTHY });
    expect(h.bridge.current.phase).toBe("error");
    expect(h.bridge.current.error?.code).toBe("MEET_SESSION_UNHEALTHY");
    expect(h.events.at(-1)).toEqual({ state: "failed", reason: "SESSION_UNHEALTHY" });
  });

  it("ends when the conference ends while live", async () => {
    const h = harness();
    await h.bridge.start();
    h.openSocket();
    h.session.set({ connectionState: MeetConnectionState.JOINED });
    h.session.set({ connectionState: MeetConnectionState.DISCONNECTED, disconnectReason: MeetDisconnectReason.CONFERENCE_ENDED });
    expect(h.bridge.current.phase).toBe("ended");
    expect(h.events.at(-1)).toEqual({ state: "disconnected", reason: "CONFERENCE_ENDED" });
    expect(JSON.parse(h.sent.at(-1)!)).toEqual({ event: "stop" });
  });
});
