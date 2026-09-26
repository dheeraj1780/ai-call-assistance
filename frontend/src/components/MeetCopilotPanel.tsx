import { useEffect, useRef, useState } from "react";

import { MEET_ERROR_HELP } from "../lib/meet/meetingCode";
import { MeetCopilotBridge, browserDeps, type BridgeDeps, type BridgeStatus } from "../lib/meet/bridge";
import { Alert, Button } from "./ui";

const PHASE_TEXT: Record<BridgeStatus["phase"], string> = {
  idle: "Not connected to the meeting.",
  connecting: "Connecting to Google Meet…",
  waiting: "Waiting for Google to admit the copilot (the meeting host/initiator may need to approve)…",
  live: "● Receiving meeting audio",
  ended: "Copilot left the meeting.",
  error: "Could not connect.",
};

/** Attaches the live copilot to the Google Meet of a started call (Meet Media API, listen-only).
 * The meeting itself is unaffected by anything here. */
export function MeetCopilotPanel({
  callId,
  callLive,
  meetingUrl,
  depsFactory = browserDeps,
}: {
  callId: string;
  callLive: boolean;
  meetingUrl: string | null;
  depsFactory?: () => Promise<BridgeDeps>;
}) {
  const bridge = useRef<MeetCopilotBridge | null>(null);
  const [status, setStatus] = useState<BridgeStatus | null>(null);

  useEffect(() => () => void bridge.current?.stop(), []);
  useEffect(() => {
    if (!callLive) void bridge.current?.stop();
  }, [callLive]);

  async function connect() {
    const deps = await depsFactory();
    const b = new MeetCopilotBridge(callId, deps, setStatus);
    bridge.current = b;
    await b.start();
  }

  const phase = status?.phase ?? "idle";
  const active = phase === "connecting" || phase === "waiting" || phase === "live";
  const error = status?.error;
  return (
    <div className="space-y-2 rounded-lg border border-slate-200 bg-white p-3" aria-label="Google Meet copilot">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0 text-sm">
          <p className="font-medium text-slate-800">Google Meet copilot</p>
          <p className={phase === "live" ? "text-emerald-700" : "text-slate-500"} aria-live="polite">
            {PHASE_TEXT[phase]}
            {phase === "live" && status ? ` · ${status.audioTracks} audio stream(s)` : ""}
          </p>
        </div>
        <div className="flex gap-2">
          {meetingUrl ? (
            <a className="text-sm underline" href={meetingUrl} target="_blank" rel="noreferrer">
              Open meeting
            </a>
          ) : null}
          {active ? (
            <Button variant="secondary" onClick={() => void bridge.current?.stop()}>
              Disconnect copilot
            </Button>
          ) : (
            <Button onClick={() => void connect()} disabled={!callLive}>
              {phase === "idle" ? "Connect copilot to meeting" : "Reconnect"}
            </Button>
          )}
        </div>
      </div>
      {active ? (
        <p className="rounded bg-sky-50 px-2 py-1 text-xs text-sky-800">
          Live meeting audio is being processed by CallCopilot for transcription and suggestions. It is not recorded or
          stored. Google shows meeting participants that an app is accessing the meeting.
        </p>
      ) : null}
      {!callLive && phase === "idle" ? (
        <p className="text-xs text-slate-500">Start the call first, join the Google Meet yourself, then connect the copilot.</p>
      ) : null}
      {error ? (
        <Alert>
          {MEET_ERROR_HELP[error.code] ?? error.message}
          <span className="ml-1 text-xs opacity-70">({error.code})</span>
        </Alert>
      ) : null}
    </div>
  );
}
