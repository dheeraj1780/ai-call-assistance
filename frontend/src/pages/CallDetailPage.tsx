import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { ActionItemList, AddActionItemForm } from "../components/ActionItemList";
import { ScheduleEventForm } from "../components/ScheduleEventForm";
import { Badge, Card, QueryState, SelectField, TextArea } from "../components/common";
import { Alert, Button } from "../components/ui";
import { CALL_OUTCOMES, crm, formatDateTime, formatDuration, label, type Call, type CallStatus } from "../lib/crm";
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
              {["INITIATED", "RINGING", "CONNECTED", "ACTIVE"].includes(call.data.status) ? (
                <Link
                  to={`/calls/${id}/live`}
                  className="inline-flex items-center rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white"
                >
                  Open live call
                </Link>
              ) : null}
              {call.data.status === "PLANNED" ? (
                <Link
                  to={`/calls/${id}/prepare`}
                  className="inline-flex items-center rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white"
                >
                  Prepare &amp; start
                </Link>
              ) : null}
              {(MANUAL_ACTIONS[call.data.status] ?? []).map((a) => (
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
          {update.error ? <Alert>{errorMessage(update.error)}</Alert> : null}

          <Card title="Details">
            <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              <Field name="Objective" value={call.data.objective} />
              <Field name="Desired outcome" value={call.data.desired_outcome} />
              <Field name="Scheduled" value={formatDateTime(call.data.scheduled_at)} />
              <Field name="Started" value={formatDateTime(call.data.started_at)} />
              <Field name="Ended" value={formatDateTime(call.data.ended_at)} />
              <Field name="Duration" value={formatDuration(call.data.duration_seconds)} />
            </dl>
          </Card>

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
