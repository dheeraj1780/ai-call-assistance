/**
 * Google Meet -> CallCopilot live pipeline, running in the salesperson's browser.
 *
 *   Google's Meet Media API reference client (vendored, unchanged logic)
 *     - offer: 3 receive-only audio transceivers + session-control / media-stats / media-entries /
 *       participants data channels; media stats uploaded as Google requests
 *     - signalling: our API (POST /calls/{id}/google-meet/connect) calls connectActiveConference
 *       with the user's token; the browser never sees a Google token
 *   -> the 3 virtual audio streams are mixed by Web Audio at 16 kHz (mono)
 *   -> 16-bit PCM, 20 ms frames -> existing media WebSocket (track "mixed")
 *   -> MediaIngest -> Chirp 3 -> CopilotEngine -> live screen.
 *
 * Audio is only held in memory for one 20 ms frame; it is never stored or logged.
 * Speaker attribution is NOT implemented: Meet sends the loudest speakers on 3 virtual streams
 * (participant ids via CSRC); this POC mixes them into one "unknown speaker" track.
 */

import { apiFetch, wsUrl } from "../api";
import type { MediaApiCommunicationProtocol } from "../../vendor/meet-media-api/types/communication_protocol";
import type { MeetStreamTrack, MeetMediaClientRequiredConfiguration } from "../../vendor/meet-media-api/types/mediatypes";
import type { MeetSessionStatus } from "../../vendor/meet-media-api/types/meetmediaapiclient";
import type { Subscribable } from "../../vendor/meet-media-api/types/subscribable";
import { MeetConnectionState, MeetDisconnectReason } from "../../vendor/meet-media-api/types/enums";
import { PcmFramer, SAMPLE_RATE, WORKLET_SOURCE, floatToPcm16, toBase64 } from "./pcm";

export type BridgePhase = "idle" | "connecting" | "waiting" | "live" | "ended" | "error";

export interface BridgeStatus {
  phase: BridgePhase;
  error: { code: string; message: string } | null;
  audioTracks: number;
  framesSent: number;
  traceId: string | null;
}

export interface ConnectResponse {
  answer: string;
  trace_id: string | null;
  space: string;
  media_ws_path: string;
  mock: boolean;
}

export interface MeetClientLike {
  readonly sessionStatus: Subscribable<MeetSessionStatus>;
  readonly meetStreamTracks: Subscribable<MeetStreamTrack[]>;
  joinMeeting(protocol?: MediaApiCommunicationProtocol): Promise<void>;
  leaveMeeting(): Promise<void>;
}

export interface AudioSink {
  addTrack(track: MediaStreamTrack): void;
  close(): Promise<void>;
}

export interface SocketLike {
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  send(data: string): void;
  close(code?: number): void;
}

export interface BridgeDeps {
  createClient(config: MeetMediaClientRequiredConfiguration): MeetClientLike;
  createAudioSink(onSamples: (samples: Float32Array) => void): Promise<AudioSink>;
  openSocket(url: string): SocketLike;
  connect(callId: string, offer: string): Promise<ConnectResponse>;
  reportEvent(callId: string, body: { event_id: string; state: string; reason?: string }): Promise<unknown>;
  newId(): string;
}

export class MeetBridgeError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "MeetBridgeError";
  }
}

const OPEN = 1;
const REASON: Record<number, string> = {
  [MeetDisconnectReason.UNKNOWN]: "UNKNOWN",
  [MeetDisconnectReason.CLIENT_LEFT]: "CLIENT_LEFT",
  [MeetDisconnectReason.USER_STOPPED]: "USER_STOPPED",
  [MeetDisconnectReason.CONFERENCE_ENDED]: "CONFERENCE_ENDED",
  [MeetDisconnectReason.SESSION_UNHEALTHY]: "SESSION_UNHEALTHY",
};

export class MeetCopilotBridge {
  private status: BridgeStatus = { phase: "idle", error: null, audioTracks: 0, framesSent: 0, traceId: null };
  private client: MeetClientLike | null = null;
  private sink: AudioSink | null = null;
  private socket: SocketLike | null = null;
  private readonly framer: PcmFramer;
  private readonly attached = new Set<MediaStreamTrack>();
  private readonly unsubscribe: Array<() => void> = [];
  private seq = 0;
  private stopping = false;

  constructor(
    private readonly callId: string,
    private readonly deps: BridgeDeps,
    private readonly onStatus: (status: BridgeStatus) => void,
  ) {
    this.framer = new PcmFramer((frame) => this.sendFrame(frame));
  }

  get current(): BridgeStatus {
    return this.status;
  }

  private update(patch: Partial<BridgeStatus>): void {
    this.status = { ...this.status, ...patch };
    this.onStatus(this.status);
  }

  private report(state: string, reason?: string): void {
    void this.deps
      .reportEvent(this.callId, { event_id: this.deps.newId(), state, ...(reason ? { reason } : {}) })
      .catch(() => undefined); // state reporting never breaks the media path
  }

  async start(): Promise<void> {
    if (this.status.phase !== "idle") return;
    this.update({ phase: "connecting", error: null });
    try {
      this.sink = await this.deps.createAudioSink((samples) => this.framer.push(floatToPcm16(samples)));
      const client = this.deps.createClient({
        // The space is resolved server-side from the call; the token stays on the server.
        meetingSpaceId: "resolved-by-callcopilot-api",
        accessToken: "",
        numberOfVideoStreams: 0,
        enableAudioStreams: true,
      });
      this.client = client;
      this.unsubscribe.push(client.sessionStatus.subscribe((s) => this.onSession(s)));
      this.unsubscribe.push(client.meetStreamTracks.subscribe((tracks) => this.onTracks(tracks)));
      await client.joinMeeting({ connectActiveConference: (offer) => this.signal(offer) });
    } catch (err) {
      await this.fail(err);
    }
  }

  private async signal(offer: string): Promise<{ answer: string }> {
    const res = await this.deps.connect(this.callId, offer);
    if (res.mock) throw new MeetBridgeError("MEET_MOCK_MODE", "Mock mode cannot receive real meeting audio.");
    this.update({ traceId: res.trace_id });
    const socket = this.deps.openSocket(wsUrl(res.media_ws_path));
    socket.onopen = () =>
      socket.send(JSON.stringify({ event: "start", format: { encoding: "linear16", sample_rate: SAMPLE_RATE } }));
    this.socket = socket;
    return { answer: res.answer };
  }

  private onSession(s: MeetSessionStatus): void {
    if (s.connectionState === MeetConnectionState.WAITING) {
      this.update({ phase: "waiting" });
      this.report("waiting");
    } else if (s.connectionState === MeetConnectionState.JOINED) {
      this.update({ phase: "live" });
      this.report("joined");
    } else if (s.connectionState === MeetConnectionState.DISCONNECTED && this.status.phase !== "ended") {
      const reason = REASON[s.disconnectReason ?? MeetDisconnectReason.UNKNOWN] ?? "UNKNOWN";
      const wasLive = this.status.phase === "live";
      this.report(wasLive || this.stopping ? "disconnected" : "failed", reason);
      void this.teardown();
      this.update(
        wasLive || this.stopping
          ? { phase: "ended" }
          : { phase: "error", error: { code: `MEET_${reason}`, message: `Google ended the media session (${reason}).` } },
      );
    }
  }

  private onTracks(tracks: MeetStreamTrack[]): void {
    for (const t of tracks) {
      const track = t.mediaStreamTrack;
      if (track.kind !== "audio" || this.attached.has(track)) continue;
      this.attached.add(track);
      this.sink?.addTrack(track);
    }
    this.update({ audioTracks: this.attached.size });
  }

  private sendFrame(frame: Uint8Array): void {
    const socket = this.socket;
    if (!socket || socket.readyState !== OPEN) return; // dropped, never buffered to disk
    this.seq += 1;
    socket.send(JSON.stringify({ event: "media", track: "mixed", seq: this.seq, payload: toBase64(frame) }));
    this.update({ framesSent: this.seq });
  }

  /** End the copilot for this meeting: leave the Media API session (the meeting goes on). */
  async stop(): Promise<void> {
    if (this.status.phase === "ended" || this.status.phase === "idle") return;
    this.stopping = true;
    try {
      if (this.client && this.status.phase !== "error") {
        await Promise.race([this.client.leaveMeeting(), new Promise((r) => setTimeout(r, 3000))]);
      }
    } catch {
      // leaving is best effort; the session times out on Google's side
    }
    // Google may already have reported the disconnect while we were leaving.
    if (this.current.phase !== "ended") this.report("disconnected", "CLIENT_LEFT");
    await this.teardown();
    this.update({ phase: "ended" });
  }

  private async teardown(): Promise<void> {
    this.unsubscribe.splice(0).forEach((u) => u());
    this.framer.reset();
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState === OPEN) {
      socket.send(JSON.stringify({ event: "stop" }));
      socket.close(1000);
    }
    const sink = this.sink;
    this.sink = null;
    await sink?.close().catch(() => undefined);
  }

  private async fail(err: unknown): Promise<void> {
    const code =
      err instanceof MeetBridgeError
        ? err.code
        : typeof err === "object" && err !== null && "code" in err
          ? String((err as { code: unknown }).code)
          : "MEET_CONNECT_FAILED";
    const message = err instanceof Error ? err.message : "Could not connect to the meeting.";
    await this.teardown();
    this.update({ phase: "error", error: { code, message } });
  }
}

// ---- browser implementations -------------------------------------------------------------------

async function createWebAudioSink(onSamples: (samples: Float32Array) => void): Promise<AudioSink> {
  // The AudioContext resamples Opus (48 kHz) to 16 kHz and sums all connected tracks (mono mix).
  const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
  const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
  try {
    await ctx.audioWorklet.addModule(url);
  } finally {
    URL.revokeObjectURL(url);
  }
  const tap = new AudioWorkletNode(ctx, "meet-pcm-tap", { channelCount: 1, channelCountMode: "explicit" });
  tap.port.onmessage = (e: MessageEvent<Float32Array>) => onSamples(e.data);
  const silent = ctx.createGain();
  silent.gain.value = 0; // keeps the graph running; the meeting audio is not played back here
  tap.connect(silent).connect(ctx.destination);
  await ctx.resume();
  const elements: HTMLAudioElement[] = [];
  return {
    addTrack(track: MediaStreamTrack) {
      const stream = new MediaStream([track]);
      // Chromium only pulls remote WebRTC audio into Web Audio when a media element consumes it.
      const el = new Audio();
      el.muted = true;
      el.srcObject = stream;
      void el.play().catch(() => undefined);
      elements.push(el);
      ctx.createMediaStreamSource(stream).connect(tap);
    },
    async close() {
      elements.forEach((el) => {
        el.srcObject = null;
      });
      tap.port.onmessage = null;
      await ctx.close();
    },
  };
}

export async function browserDeps(): Promise<BridgeDeps> {
  // Loaded lazily: only the Google Meet live screen ever needs Google's client.
  const { MeetMediaApiClientImpl } = await import("../../vendor/meet-media-api/internal/meetmediaapiclient_impl");
  return {
    createClient: (config) => new MeetMediaApiClientImpl(config),
    createAudioSink: createWebAudioSink,
    openSocket: (url) => new WebSocket(url),
    connect: (callId, offer) =>
      apiFetch<ConnectResponse>(`/api/v1/calls/${callId}/google-meet/connect`, { method: "POST", body: { offer } }),
    reportEvent: (callId, body) =>
      apiFetch(`/api/v1/calls/${callId}/google-meet/events`, { method: "POST", body }),
    newId: () => crypto.randomUUID(),
  };
}
