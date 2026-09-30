import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { ActionItemList, AddActionItemForm } from "../components/ActionItemList";
import { PostCallPanel } from "../components/PostCallPanel";
import { ScheduleEventForm } from "../components/ScheduleEventForm";
import { Badge, Card, QueryState, SelectField, TextArea } from "../components/common";
import { Alert, Button } from "../components/ui";
import {
  CALL_OUTCOMES,
  LIVE_CALL_STATUSES,
  canReopen,
  crm,
  explainCallError,
  formatDateTime,
  formatDuration,
  label,
  type Call,
  type CallStatus,
} from "../lib/crm";
import { liveApi, type Segment } from "../lib/live";
import { errorMessage } from "../lib/errors";
import { memberName, useMembers } from "../lib/members";

/** Manual status actions for calls tracked without telephony. */
const MANUAL_ACTIONS: Partial<Record<CallStatus, { to: CallStatus; text: string }[]>> = {
  PLANNED: [
    { to: "ACTIVE", text: "Mark started" },
    { to: "COMPLETED", text: "Log as completed" },
    { to: "NO_ANSWER", text: "No answer" },
    { to: "CANCELLED", text: "Cancel" },
  ],
  ACTIVE: [
    { to: "COMPLETED", text: "Mark completed" },
    { to: "NO_ANSWER", text: "No answer" },
  ],
};

export function CallDetailPage() {
  const { id = "" } = useParams();
  const queryClient = useQueryClient();
  const members = useMembers();
  const call = useQuery({ queryKey: ["call", id], queryFn: () => crm.getCall(id) });
  const items = useQuery({
    queryKey: ["action-items", { callId: id }],
    queryFn: () => crm.listActionItems({ call_id: id, limit: 100 }),
  });
  const end = useMutation({
    mutationFn: () => crm.endCall(id),
    onSuccess: (updated) => {
      queryClient.setQueryData(["call", id], updated);
      queryClient.invalidateQueries({ queryKey: ["calls"] });
    },
  });
  const reopen = useMutation({
    mutationFn: () => crm.reopenCall(id),
    onSuccess: (updated) => {
      queryClient.setQueryData(["call", id], updated);
      queryClient.invalidateQueries({ queryKey: ["calls"] });
    },
  });
  const update = useMutation({
    mutationFn: (body: Partial<Call>) => crm.updateCall(id, body),
    onSuccess: (updated) => {
      queryClient.setQueryData(["call", id], updated);
      queryClient.invalidateQueries({ queryKey: ["calls"] });
      queryClient.invalidateQueries({ queryKey: ["timeline"] });
    },
  });

  return (
    <QueryState isPending={call.isPending} error={call.error}>
      {call.data ? (
        <div className="space-y-4">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-semibold text-slate-900">
                  Call with{" "}
                  <Link className="underline" to={`/contacts/${call.data.contact.id}`}>
                    {call.data.contact.name}
                  </Link>
                </h1>
                <Badge value={call.data.status} />
              </div>
              <p className="text-sm text-slate-500">
                {memberName(members.data?.items, call.data.user_id)} · created {formatDateTime(call.data.created_at)}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {LIVE_CALL_STATUSES.has(call.data.status) ? (
                <>
                  <Link
                    to={`/calls/${id}/live`}
                    className="inline-flex items-center rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white"
                  >
                    Open live call
                  </Link>
                  {call.data.provider ? (
                    <Button
                      variant="secondary"
                      disabled={end.isPending || call.data.status === "ENDING"}
                      onClick={() => end.mutate()}
                    >
                      {call.data.status === "ENDING" ? "Ending…" : "End call"}
                    </Button>
                  ) : null}
                </>
              ) : null}
              {canReopen(call.data) ? (
                <Button disabled={reopen.isPending} onClick={() => reopen.mutate()}>
                  Reopen to fix and retry
                </Button>
              ) : null}
              {call.data.status === "PLANNED" ? (
                <Link
                  to={`/calls/${id}/prepare`}
                  className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
                >
                  Edit, prepare &amp; start
                </Link>
              ) : null}
              {(call.data.provider && call.data.status !== "PLANNED" ? [] : (MANUAL_ACTIONS[call.data.status] ?? [])).map((a) => (
                <Button
                  key={a.to}
                  variant={a.to === "ACTIVE" ? "primary" : "secondary"}
                  disabled={update.isPending}
                  onClick={() => update.mutate({ status: a.to })}
                >
                  {a.text}
                </Button>
              ))}
            </div>
          </div>
          {update.error || end.error || reopen.error ? (
            <Alert>{errorMessage(update.error ?? end.error ?? reopen.error)}</Alert>
          ) : null}
          {canReopen(call.data) && call.data.telephony_error ? (
            <Alert>
              {explainCallError(call.data.telephony_error)} You can reopen the call, correct its details and start it again.
            </Alert>
          ) : null}

          <Card title="Details">
            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <Field name="Objective" value={call.data.objective} />
              <Field name="Desired outcome" value={call.data.desired_outcome} />
              <Field name="Scheduled" value={formatDateTime(call.data.scheduled_at)} />
              <Field name="Started" value={formatDateTime(call.data.started_at)} />
              <Field name="Ended" value={formatDateTime(call.data.ended_at)} />
              <Field name="Duration" value={formatDuration(call.data.duration_seconds)} />
              {call.data.meeting_url ? <Field name="Meeting link" value={call.data.meeting_url} /> : null}
            </dl>
          </Card>

          {["COMPLETED", "NO_ANSWER", "FAILED", "CANCELLED"].includes(call.data.status) && call.data.provider ? (
            <PostCallPanel callId={id} />
          ) : null}

          {call.data.started_at && call.data.provider && call.data.transcript_persistence === "PERSISTED" ? (
            <TranscriptCard callId={id} canEdit={!LIVE_CALL_STATUSES.has(call.data.status)} />
          ) : null}

          {call.data.status === "COMPLETED" ? <OutcomeCard call={call.data} onSave={(b) => update.mutate(b)} saving={update.isPending} /> : null}

          {call.data.status === "COMPLETED" || call.data.status === "NO_ANSWER" ? (
            <Card title="Schedule follow-up (Google Calendar)">
              <ScheduleEventForm
                defaultTitle={`Follow-up with ${call.data.contact.name}`}
                contactId={call.data.contact.id}
                callId={id}
              />
            </Card>
          ) : null}

          <Card title="Action items">
            <div className="space-y-3">
              <AddActionItemForm callId={id} />
              <QueryState isPending={items.isPending} error={items.error}>
                <ActionItemList items={items.data?.items ?? []} />
              </QueryState>
            </div>
          </Card>
        </div>
      ) : null}
    </QueryState>
  );
}

function Field({ name, value }: { name: string; value: string | null }) {
  return (
    <div>
      <dt className="text-slate-500">{name}</dt>
      <dd className="whitespace-pre-line text-slate-900">{value || "—"}</dd>
    </div>
  );
}

function OutcomeCard({ call, onSave, saving }: { call: Call; onSave: (b: Partial<Call>) => void; saving: boolean }) {
  const [outcome, setOutcome] = useState(call.outcome ?? "");
  const [notes, setNotes] = useState(call.outcome_notes ?? "");
  const [nextStep, setNextStep] = useState(call.next_step ?? "");
  return (
    <Card title="Outcome">
      <div className="grid gap-3">
        <SelectField
          label="Outcome"
          value={outcome}
          onChange={(e) => setOutcome(e.target.value)}
          options={[{ value: "", label: "Not set" }, ...CALL_OUTCOMES.map((o) => ({ value: o, label: label(o) }))]}
        />
        <TextArea label="Outcome notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <TextArea label="Next step" value={nextStep} onChange={(e) => setNextStep(e.target.value)} />
        <div>
          <Button
            disabled={saving}
            onClick={() =>
              onSave({
                outcome: (outcome || null) as Call["outcome"],
                outcome_notes: notes || null,
                next_step: nextStep || null,
              })
            }
          >
            Save outcome
          </Button>
        </div>
      </div>
    </Card>
  );
}

/** The stored transcript. A person can correct wording; the speech-to-text output is kept as the
 * original so what was heard and what was fixed stay distinguishable. */
function TranscriptCard({ callId, canEdit }: { callId: string; canEdit: boolean }) {
  const queryClient = useQueryClient();
  const transcript = useQuery({ queryKey: ["transcript", callId], queryFn: () => liveApi.transcript(callId) });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const save = useMutation({
    mutationFn: (seg: Segment) => liveApi.editSegment(callId, seg.id, draft.trim()),
    onSuccess: () => {
      setEditing(null);
      queryClient.invalidateQueries({ queryKey: ["transcript", callId] });
    },
  });
  const speaker = (s: Segment) => (s.speaker === "CUSTOMER" ? "Customer" : s.speaker === "SALES_REP" ? "You" : "Speaker (not identified)");
  return (
    <Card title="Transcript">
      <p className="mb-2 text-xs text-slate-500">
        Audio is never recorded; only this text is kept, according to your retention policy. You can correct any line.
      </p>
      <QueryState isPending={transcript.isPending} error={transcript.error} empty={transcript.data?.length === 0} emptyText="No transcript was captured.">
        <div className="space-y-2">
          {(transcript.data ?? []).map((s) => (
            <div key={s.id} className="text-sm">
              {editing === s.id ? (
                <form
                  className="space-y-1"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (draft.trim()) save.mutate(s);
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
                  {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
                </form>
              ) : (
                <p>
                  <span className="mr-1 font-medium text-slate-500">{speaker(s)}:</span>
                  <span className="text-slate-900">{s.text}</span>
                  {s.edited ? (
                    <span className="ml-1 text-[10px] text-sky-700" title={s.original_text ? `Heard: ${s.original_text}` : undefined}>
                      (edited)
                    </span>
                  ) : null}
                  {canEdit ? (
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
                  ) : null}
                </p>
              )}
            </div>
          ))}
        </div>
      </QueryState>
    </Card>
  );
}
