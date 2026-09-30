import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { MeetCopilotPanel } from "../components/MeetCopilotPanel";
import { Link, useNavigate, useParams } from "react-router";

import { Badge, QueryState } from "../components/common";
import { Alert, Button } from "../components/ui";
import { canReopen, explainCallError, label } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import {
  LIVE_STATUSES,
  TRANSCRIPT_DELAY_S,
  finalisingMessage,
  transcriptHealth,
  NOTE_KINDS,
  TERMINAL_STATUSES,
  liveApi,
  retentionNotice,
  type Segment,
  visibleInsights,
  type CallNote,
  type Insight,
  type LiveState,
  type NoteKind,
} from "../lib/live";
import { prepApi, type AgendaItem, type AgendaStatus } from "../lib/prep";
import { useLiveCall, type Connection } from "../lib/useLiveCall";

type Tab = "copilot" | "transcript" | "agenda" | "notes";

export function LiveCallPage() {
  const { id = "" } = useParams();
  const { state, error, connection, reload, patch } = useLiveCall(id);
  const [tab, setTab] = useState<Tab>("copilot");
  const [actionError, setActionError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      await reload();
    } catch (e) {
      setActionError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <QueryState isPending={!state && !error} error={state ? null : error}>
      {state ? (
        <div className="space-y-3">
          <Header state={state} connection={connection} busy={busy} run={run} callId={id} />
          {actionError ? <Alert>{errorMessage(actionError)}</Alert> : null}
          {(state.channel ?? state.call.channel) === "GOOGLE_MEET" ? (
            <MeetCopilotPanel
              callId={id}
              callLive={LIVE_STATUSES.has(state.call.status)}
              meetingUrl={state.call.meeting_url ?? null}
            />
          ) : null}
          <PipelineBanner state={state} />
          <TranscriptHealth state={state} />
          <FinalisingNotice state={state} />
          <nav className="flex gap-1 border-b border-slate-200 lg:hidden" aria-label="Live call sections">
            {(["copilot", "transcript", "agenda", "notes"] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                aria-current={tab === t ? "page" : undefined}
                className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                  tab === t ? "border-slate-900 font-medium" : "border-transparent text-slate-500"
                }`}
              >
                {label(t)}
              </button>
            ))}
          </nav>
          <div className="grid gap-4 lg:grid-cols-12">
            <div className={`${tab === "agenda" ? "" : "hidden"} lg:col-span-3 lg:block`}>
              <AgendaPanel callId={id} agenda={state.agenda} onChange={(item) => patch((s) => ({ ...s, agenda: s.agenda.map((a) => (a.id === item.id ? item : a)) }))} />
              <div className="mt-4">
                <ContextPanel callId={id} />
              </div>
            </div>
            <div className={`${tab === "transcript" ? "" : "hidden"} lg:col-span-5 lg:block`}>
              <TranscriptPanel state={state} callId={id} patch={patch} />
            </div>
            <div className={`${tab === "copilot" || tab === "notes" ? "" : "hidden"} space-y-4 lg:col-span-4 lg:block`}>
              <div className={`${tab === "copilot" ? "" : "hidden"} lg:block`}>
                <CopilotPanel callId={id} processing={state.pipeline.copilot_processing === true} insights={state.insights} onDismiss={(iid) => patch((s) => ({ ...s, insights: s.insights.filter((i) => i.id !== iid) }))} />
              </div>
              <div className={`${tab === "notes" ? "" : "hidden"} lg:block`}>
                <NotesPanel callId={id} notes={state.notes} patch={patch} />
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </QueryState>
  );
}

function useElapsed(startedAt: string | null, endedAt: string | null): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt || endedAt) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [startedAt, endedAt]);
  if (!startedAt) return "00:00";
  const end = endedAt ? new Date(endedAt).getTime() : now;
  const secs = Math.max(0, Math.floor((end - new Date(startedAt).getTime()) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function channelLabel(channel: string | undefined): string {
  if (channel === "TEAMS") return "Microsoft Teams";
  if (channel === "GOOGLE_MEET") return "Google Meet";
  return "Phone";
}

function audioState(state: LiveState): string | null {
  const s = state.call.status;
  if (!["CONNECTED", "ACTIVE"].includes(s)) return null;
  if (state.pipeline.media === "unavailable") return "Audio: not reaching the copilot";
  if (s === "ACTIVE") return "Audio: receiving";
  return "Audio: waiting for the stream";
}

function Header({
  state,
  connection,
  busy,
  run,
  callId,
}: {
  state: LiveState;
  connection: Connection;
  busy: boolean;
  run: (fn: () => Promise<unknown>) => void;
  callId: string;
}) {
  const navigate = useNavigate();
  const { call } = state;
  const elapsed = useElapsed(call.started_at, call.ended_at);
  const live = LIVE_STATUSES.has(call.status);
  const ending = call.status === "ENDING";
  const ended = TERMINAL_STATUSES.has(call.status);
  const audio = audioState(state);
  const failure = explainCallError(call.telephony_error);
  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            <Link to={`/calls/${callId}`} className="underline">
              Call record
            </Link>{" "}
            · Live call · Channel: {channelLabel(state.channel ?? call.channel)}
          </p>
          <div className="flex items-center gap-2">
            <Link to={`/contacts/${call.contact.id}`} className="truncate text-lg font-semibold text-slate-900">
              {call.contact.name}
            </Link>
            <Badge value={call.status} />
            <span className="font-mono text-lg tabular-nums text-slate-700" aria-label="Call duration">
              {elapsed}
            </span>
          </div>
          {call.objective ? <p className="truncate text-sm text-slate-500">{call.objective}</p> : null}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`text-xs ${connection === "live" ? "text-emerald-700" : "text-amber-700"}`}
            title="Connection between this screen and the server. The call itself does not depend on it: reconnecting resumes it."
          >
            {connection === "live"
              ? "● Live"
              : connection === "offline"
                ? "○ Offline"
                : connection === "connecting"
                  ? "○ Connecting…"
                  : "○ Reconnecting…"}
          </span>
          {audio ? <span className="text-xs text-slate-500">{audio}</span> : null}
          {call.status === "PLANNED" ? (
            <>
              <Link
                to={`/calls/${callId}/prepare`}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700"
              >
                Edit details
              </Link>
              <Button disabled={busy} onClick={() => run(() => liveApi.start(callId))}>
                Start call
              </Button>
            </>
          ) : null}
          {call.status === "INITIATED" && state.simulation_available ? (
            <Button variant="secondary" disabled={busy} onClick={() => run(() => liveApi.simulate(callId))}>
              Simulate conversation (mock)
            </Button>
          ) : null}
          {live ? (
            <button
              type="button"
              disabled={busy || ending}
              onClick={() => run(() => liveApi.end(callId))}
              className="rounded-md bg-rose-700 px-4 py-2 text-sm font-semibold text-white hover:bg-rose-800 disabled:opacity-60"
            >
              {ending ? "Ending…" : "End call"}
            </button>
          ) : null}
          {ended ? (
            <Link to={`/calls/${callId}`} className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white">
              View summary
            </Link>
          ) : null}
        </div>
      </div>
      {ending ? (
        <p role="status" className="text-xs text-slate-500">
          Ending the call… waiting for the provider to confirm; it will be closed here automatically if it does not.
        </p>
      ) : null}
      {ended && canReopen(call) ? (
        <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">
          <p>
            This call did not connect{failure ? `: ${failure}` : "."} Nothing was recorded. You can correct the details (for example
            the meeting link) and try again.
          </p>
          <div className="mt-2">
            <Button
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await liveApi.reopen(callId);
                  navigate(`/calls/${callId}/prepare`);
                })
              }
            >
              Fix details and retry
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

const MEDIA_REASON: Record<string, string> = {
  recording_status_failed:
    "Teams did not confirm the recording status, so no call audio is processed (compliance).",
  media_socket_connect_failed: "The Teams media gateway could not reach the server.",
  media_stream_disabled:
    "Real-time audio is not enabled for phone calls (an admin can enable it in Settings → Integrations → Plivo).",
  stream_start_failed: "The phone provider could not start the audio stream.",
};

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 2000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function TranscriptHealth({ state }: { state: LiveState }) {
  const now = useNow(state.call.status === "ACTIVE");
  const health = transcriptHealth(state, now);
  if (!health) return null;
  const text = {
    receiving: "● Receiving transcript",
    waiting: "○ Listening – the transcript appears when someone speaks",
    delayed: `○ No transcript for ${TRANSCRIPT_DELAY_S}+ seconds – if people are talking, transcription may be delayed`,
  }[health];
  return (
    <p className={`text-xs ${health === "delayed" ? "text-amber-700" : "text-slate-500"}`} aria-live="polite">
      {text}
    </p>
  );
}

function FinalisingNotice({ state }: { state: LiveState }) {
  const text = finalisingMessage(state);
  if (!text) return null;
  return (
    <p className="text-xs text-slate-500" aria-live="polite">
      {text}
    </p>
  );
}

function PipelineBanner({ state }: { state: LiveState }) {
  const messages: string[] = [];
  const teams = state.channel === "TEAMS" || state.call.channel === "TEAMS";
  if (state.pipeline.media === "unavailable")
    messages.push(
      `${teams ? "Teams meeting audio" : "Call audio"} is not reaching the copilot. ${
        MEDIA_REASON[state.pipeline.media_reason ?? ""] ?? ""
      } The ${teams ? "meeting" : "call"} itself continues.`,
    );
  if (state.pipeline.stt === "unavailable")
    messages.push(`Live transcription is interrupted. Your ${teams ? "Teams meeting" : "phone call"} continues normally.`);
  if (state.pipeline.copilot === "degraded")
    messages.push("AI suggestions are temporarily limited. Transcript and notes keep working.");
  const liveOnly = (state.transcript_persistence ?? state.call.transcript_persistence ?? "PERSISTED") !== "PERSISTED";
  return (
    <>
      {messages.length ? (
        <div role="status" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {messages.join(" ")}
        </div>
      ) : null}
      <p
        className={`text-xs ${liveOnly ? "rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-800" : "text-slate-500"}`}
        aria-label="Data handling"
      >
        {retentionNotice(state)}
      </p>
    </>
  );
}

const STATUS_ICON: Record<AgendaStatus, string> = {
  COMPLETED: "✓",
  IN_PROGRESS: "◐",
  NOT_STARTED: "○",
  SKIPPED: "–",
};
const NEXT_STATUS: Record<AgendaStatus, AgendaStatus> = {
  NOT_STARTED: "COMPLETED",
  IN_PROGRESS: "COMPLETED",
  COMPLETED: "NOT_STARTED",
  SKIPPED: "NOT_STARTED",
};

function AgendaPanel({ callId, agenda, onChange }: { callId: string; agenda: AgendaItem[]; onChange: (a: AgendaItem) => void }) {
  const [error, setError] = useState<unknown>(null);
  const current = agenda.find((a) => a.status === "IN_PROGRESS");
  async function toggle(item: AgendaItem, status: AgendaStatus) {
    try {
      onChange(await prepApi.setItemStatus(callId, item.id, status));
    } catch (e) {
      setError(e);
    }
  }
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3" aria-label="Agenda">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Agenda</h2>
      {agenda.length === 0 ? <p className="text-sm text-slate-500">No agenda for this call.</p> : null}
      <ul className="space-y-1">
        {agenda.map((item) => (
          <li key={item.id} className={`rounded px-2 py-1 ${item === current ? "bg-amber-50" : ""}`}>
            <div className="flex items-start gap-2">
              <button
                aria-label={`${item.title}: ${label(item.status)}. Change status`}
                className="w-5 text-center font-mono"
                onClick={() => toggle(item, NEXT_STATUS[item.status])}
              >
                {STATUS_ICON[item.status]}
              </button>
              <div className="min-w-0 flex-1">
                <p className={`text-sm ${item.status === "COMPLETED" ? "text-slate-500" : "text-slate-900"}`}>{item.title}</p>
                {item.status === "NOT_STARTED" && item.question ? (
                  <p className="text-xs text-slate-500">{item.question}</p>
                ) : null}
              </div>
              {item.status !== "SKIPPED" && item.status !== "COMPLETED" ? (
                <button className="text-xs text-slate-400 underline" onClick={() => toggle(item, "SKIPPED")}>
                  skip
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
      {error ? <Alert>{errorMessage(error)}</Alert> : null}
    </section>
  );
}

/** Customer context from the planning data (previous calls, known requirements and objections). */
function ContextPanel({ callId }: { callId: string }) {
  const prep = useQuery({ queryKey: ["prep", callId], queryFn: () => prepApi.prep(callId) });
  const data = prep.data;
  if (!data) return null;
  const known = [...data.known_requirements, ...data.known_objections].slice(0, 6);
  const last = data.previous_calls[0];
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-3" aria-label="Customer context">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Customer context</h2>
      <p className="text-sm font-medium text-slate-900">
        {data.contact.name}
        {data.contact.organization ? <span className="font-normal text-slate-500"> · {data.contact.organization}</span> : null}
      </p>
      {last ? (
        <p className="mt-1 text-xs text-slate-600">
          Last call: {last.summary ?? last.objective ?? "no summary"}
          {last.next_step ? ` — next step: ${last.next_step}` : ""}
        </p>
      ) : (
        <p className="mt-1 text-xs text-slate-500">No earlier calls.</p>
      )}
      {known.length ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-slate-600">
          {known.map((k, i) => (
            <li key={i}>
              {label(k.kind)}: {k.text}
            </li>
          ))}
        </ul>
      ) : null}
      {data.open_action_items.length ? (
        <p className="mt-1 text-xs text-slate-600">{data.open_action_items.length} open action item(s).</p>
      ) : null}
    </section>
  );
}

function speakerLabel(speaker: Segment["speaker"]): string {
  if (speaker === "CUSTOMER") return "Customer";
  if (speaker === "SALES_REP") return "You";
  // Mixed audio (Teams/Meet): who spoke is not known. Shown as such, never guessed.
  return "Speaker (not identified)";
}

function TranscriptPanel({
  state,
  callId,
  patch,
}: {
  state: LiveState;
  callId: string;
  patch: (fn: (s: LiveState) => LiveState) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [state.transcript.length, state.partial]);

  async function save(seg: Segment) {
    setError(null);
    try {
      const updated = await liveApi.editSegment(callId, seg.id, draft.trim());
      patch((s) => ({ ...s, transcript: s.transcript.map((x) => (x.id === seg.id ? { ...x, ...updated } : x)) }));
      setEditing(null);
    } catch (e) {
      setError(e);
    }
  }
  return (
    <section className="flex h-[60vh] flex-col rounded-lg border border-slate-200 bg-white" aria-label="Live transcript">
      <h2 className="border-b border-slate-100 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Live transcript
      </h2>
      <div className="flex-1 space-y-2 overflow-y-auto px-3 py-2">
        {state.transcript.length === 0 && !state.partial ? (
          <p className="text-sm text-slate-500">
            {state.call.status === "PLANNED" ? "Start the call to see the live transcript." : "Waiting for speech…"}
          </p>
        ) : null}
        {state.transcript.map((s) => (
          <div key={s.id} className="text-sm leading-snug">
            {editing === s.id ? (
              <form
                className="space-y-1"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (draft.trim()) void save(s);
                }}
              >
                <textarea
                  aria-label="Edit transcript line"
                  className="block w-full rounded border border-slate-300 px-2 py-1 text-sm"
                  rows={2}
                  maxLength={5000}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <div className="flex gap-2 text-xs">
                  <button className="underline">Save</button>
                  <button type="button" className="text-slate-500 underline" onClick={() => setEditing(null)}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <p>
                <span className={`mr-1 font-medium ${s.speaker === "CUSTOMER" ? "text-sky-700" : "text-slate-500"}`}>
                  {speakerLabel(s.speaker)}:
                </span>
                <span className="text-slate-900">{s.text}</span>
                {s.edited ? (
                  <span className="ml-1 text-[10px] text-sky-700" title={s.original_text ? `Heard: ${s.original_text}` : undefined}>
                    (edited)
                  </span>
                ) : null}
                <button
                  aria-label={`Edit transcript line: ${s.text}`}
                  className="ml-1 text-xs text-slate-400"
                  onClick={() => {
                    setEditing(s.id);
                    setDraft(s.text);
                  }}
                >
                  ✎
                </button>
              </p>
            )}
          </div>
        ))}
        {state.partial ? (
          <p className="text-sm italic text-slate-400" aria-live="polite">
            {state.partial.text}…
          </p>
        ) : null}
        {error ? <Alert>{errorMessage(error)}</Alert> : null}
        <div ref={endRef} />
      </div>
    </section>
  );
}

const CARD_STYLE: Record<Insight["type"], { icon: string; tone: string; title: string }> = {
  QUESTION_SUGGESTION: { icon: "💡", tone: "border-sky-200 bg-sky-50", title: "Suggested question" },
  MISSING_AGENDA_ITEM: { icon: "⚠️", tone: "border-amber-200 bg-amber-50", title: "Not discussed yet" },
  CUSTOMER_REQUIREMENT: { icon: "📌", tone: "border-slate-200 bg-white", title: "Requirement" },
  OBJECTION: { icon: "🛑", tone: "border-rose-200 bg-rose-50", title: "Objection" },
  IMPORTANT_FACT: { icon: "📌", tone: "border-slate-200 bg-white", title: "Customer said" },
  NEXT_STEP: { icon: "➡️", tone: "border-emerald-200 bg-emerald-50", title: "Next step" },
  KNOWLEDGE_RESULT: { icon: "📚", tone: "border-indigo-200 bg-indigo-50", title: "Company knowledge" },
};

function CopilotPanel({
  callId,
  insights,
  onDismiss,
  processing = false,
}: {
  callId: string;
  insights: Insight[];
  onDismiss: (id: string) => void;
  processing?: boolean;
}) {
  const [showAll, setShowAll] = useState(false);
  const active = insights.filter((i) => i.status === "ACTIVE");
  const shown = showAll ? visibleInsights(active, 50) : visibleInsights(active);
  return (
    <section aria-label="AI copilot" className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
        AI copilot
        {processing ? <span className="ml-2 font-normal normal-case text-slate-400">analysing…</span> : null}
      </h2>
      {shown.length === 0 ? <p className="text-sm text-slate-500">Suggestions will appear here during the call.</p> : null}
      {shown.map((i) => {
        const style = CARD_STYLE[i.type];
        return (
          <article key={i.id} className={`rounded-md border p-3 ${style.tone}`}>
            <div className="flex items-start justify-between gap-2">
              <p className="text-xs font-semibold text-slate-600">
                {style.icon} {style.title}
                {i.source === "AI" ? <span className="ml-1 font-normal text-slate-400">· AI inference</span> : null}
              </p>
              <button
                aria-label="Dismiss suggestion"
                className="text-xs text-slate-400"
                onClick={() => {
                  onDismiss(i.id);
                  void liveApi.dismiss(callId, i.id).catch(() => undefined);
                }}
              >
                ✕
              </button>
            </div>
            <p className="mt-1 text-sm text-slate-900">{i.content}</p>
            {i.context ? <p className="mt-1 text-xs text-slate-500">{i.context}</p> : null}
          </article>
        );
      })}
      {active.length > 3 ? (
        <button className="text-xs text-slate-500 underline" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Show fewer" : `Show all (${active.length})`}
        </button>
      ) : null}
    </section>
  );
}

function NotesPanel({
  callId,
  notes,
  patch,
}: {
  callId: string;
  notes: CallNote[];
  patch: (fn: (s: LiveState) => LiveState) => void;
}) {
  const [kind, setKind] = useState<NoteKind>("GENERAL");
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const upsert = (note: CallNote) =>
    patch((s) => ({ ...s, notes: [...s.notes.filter((n) => n.id !== note.id), note] }));

  async function act(fn: () => Promise<void>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    }
  }
  function onAdd(e: FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    void act(async () => {
      upsert(await liveApi.addNote(callId, kind, text.trim()));
      setText("");
    });
  }
  const grouped = NOTE_KINDS.map((k) => [k, notes.filter((n) => n.kind === k)] as const).filter(([, list]) => list.length);

  return (
    <section aria-label="Structured notes" className="rounded-lg border border-slate-200 bg-white p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Structured notes</h2>
      {grouped.length === 0 ? <p className="text-sm text-slate-500">No notes yet.</p> : null}
      <dl className="space-y-2">
        {grouped.map(([k, list]) => (
          <div key={k}>
            <dt className="text-xs font-medium text-slate-500">{label(k)}</dt>
            {list.map((n) => (
              <dd key={n.id} className="flex items-start justify-between gap-2 text-sm">
                {editing === n.id ? (
                  <form
                    className="flex flex-1 gap-1"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act(async () => {
                        upsert(await liveApi.reviewNote(callId, n.id, { text: draft }));
                        setEditing(null);
                      });
                    }}
                  >
                    <input
                      aria-label="Edit note"
                      className="flex-1 rounded border border-slate-300 px-1"
                      value={draft}
                      maxLength={1000}
                      onChange={(e) => setDraft(e.target.value)}
                    />
                    <button className="text-xs underline">Save</button>
                  </form>
                ) : (
                  <span className={n.status === "SUGGESTED" ? "text-slate-600" : "text-slate-900"}>
                    {n.category ? `${label(n.category)}: ` : ""}
                    {n.text}
                    {n.status === "SUGGESTED" ? <span className="ml-1 text-xs text-amber-700">(suggested)</span> : null}
                  </span>
                )}
                {editing !== n.id ? (
                  <span className="flex shrink-0 gap-1 text-xs">
                    {n.status === "SUGGESTED" ? (
                      <button aria-label={`Confirm note ${n.text}`} onClick={() => void act(async () => upsert(await liveApi.reviewNote(callId, n.id, {})))}>
                        ✓
                      </button>
                    ) : null}
                    <button
                      aria-label={`Edit note ${n.text}`}
                      onClick={() => {
                        setEditing(n.id);
                        setDraft(n.text);
                      }}
                    >
                      ✎
                    </button>
                    <button
                      aria-label={`Remove note ${n.text}`}
                      onClick={() =>
                        void act(async () => {
                          await liveApi.deleteNote(callId, n.id);
                          patch((s) => ({ ...s, notes: s.notes.filter((x) => x.id !== n.id) }));
                        })
                      }
                    >
                      ✕
                    </button>
                  </span>
                ) : null}
              </dd>
            ))}
          </div>
        ))}
      </dl>
      <form onSubmit={onAdd} className="mt-3 flex gap-1">
        <select
          aria-label="Note type"
          className="rounded border border-slate-300 bg-white px-1 text-xs"
          value={kind}
          onChange={(e) => setKind(e.target.value as NoteKind)}
        >
          {NOTE_KINDS.map((k) => (
            <option key={k} value={k}>
              {label(k)}
            </option>
          ))}
        </select>
        <input
          aria-label="New note"
          className="flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
          placeholder="Add a note"
          value={text}
          maxLength={1000}
          onChange={(e) => setText(e.target.value)}
        />
        <Button type="submit" variant="secondary" disabled={!text.trim()}>
          Add
        </Button>
      </form>
      {error ? <Alert>{errorMessage(error)}</Alert> : null}
    </section>
  );
}
