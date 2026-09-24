import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AgendaEditor } from "../components/AgendaEditor";
import { Badge, Card, QueryState } from "../components/common";
import { Alert, Button } from "../components/ui";
import { formatDateTime, label } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { draftKey, prepApi, type AgendaDraftItem, type AgendaSuggestion, type CallPrep } from "../lib/prep";

export function CallPrepPage() {
  const { id = "" } = useParams();
  const prep = useQuery({ queryKey: ["prep", id], queryFn: () => prepApi.prep(id) });
  return (
    <QueryState isPending={prep.isPending} error={prep.error}>
      {prep.data ? <PrepView prep={prep.data} /> : null}
    </QueryState>
  );
}

function PrepView({ prep }: { prep: CallPrep }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const callId = prep.call.id;
  const editable = prep.call.status === "PLANNED";
  const [items, setItems] = useState<AgendaDraftItem[]>(() =>
    prep.agenda.map((a) => ({ key: draftKey(), title: a.title, question: a.question ?? "", source: a.source })),
  );
  const [dirty, setDirty] = useState(false);
  const [suggestion, setSuggestion] = useState<AgendaSuggestion | null>(null);

  const save = useMutation({
    mutationFn: () => prepApi.saveAgenda(callId, items.filter((i) => i.title.trim())),
    onSuccess: () => {
      setDirty(false);
      queryClient.invalidateQueries({ queryKey: ["prep", callId] });
    },
  });
  const suggest = useMutation({ mutationFn: () => prepApi.suggest(callId), onSuccess: setSuggestion });

  const change = (next: AgendaDraftItem[]) => {
    setItems(next);
    setDirty(true);
  };
  const useSuggestion = () => {
    if (!suggestion) return;
    change(
      suggestion.ai_suggested_agenda.map((s) => ({
        key: draftKey(),
        title: s.title,
        question: s.question ?? "",
        source: "AI" as const,
      })),
    );
    setSuggestion(null);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">
            Prepare call with{" "}
            <Link className="underline" to={`/contacts/${prep.contact.id}`}>
              {prep.contact.name}
            </Link>
          </h1>
          <p className="text-sm text-slate-500">{prep.contact.organization ?? ""}</p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => navigate(`/calls/${callId}`)}>
            Call record
          </Button>
          <Button
            disabled={dirty || !editable}
            title={dirty ? "Save the agenda first" : undefined}
            onClick={() => navigate(`/calls/${callId}/live`)}
          >
            Start call
          </Button>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          <Card title="Objective">
            <p className="text-sm text-slate-900">{prep.call.objective || "No objective set."}</p>
            {prep.call.desired_outcome ? (
              <p className="mt-1 text-sm text-slate-600">Desired outcome: {prep.call.desired_outcome}</p>
            ) : null}
          </Card>

          <Card
            title="Agenda"
            actions={
              editable ? (
                <Button variant="secondary" disabled={suggest.isPending} onClick={() => suggest.mutate()}>
                  {suggest.isPending ? "Thinking…" : "Suggest with AI"}
                </Button>
              ) : null
            }
          >
            {suggest.error ? (
              <div className="mb-3">
                <Alert>{errorMessage(suggest.error)}</Alert>
              </div>
            ) : null}
            {suggestion ? <SuggestionPanel suggestion={suggestion} onUse={useSuggestion} onDismiss={() => setSuggestion(null)} /> : null}
            {editable ? (
              <>
                <AgendaEditor items={items} onChange={change} />
                <div className="mt-3 flex items-center gap-3">
                  <Button disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
                    {save.isPending ? "Saving…" : "Save agenda"}
                  </Button>
                  {dirty ? <span className="text-xs text-amber-700">Unsaved changes</span> : null}
                </div>
                {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
              </>
            ) : (
              <ol className="list-decimal space-y-1 pl-5 text-sm">
                {prep.agenda.map((a) => (
                  <li key={a.id}>
                    {a.title} <Badge value={a.status} />
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>

        <div className="space-y-4 lg:col-span-2">
          <Card title="Previous calls">
            {prep.previous_calls.length === 0 ? (
              <p className="text-sm text-slate-500">No earlier calls.</p>
            ) : (
              <ul className="space-y-2 text-sm">
                {prep.previous_calls.map((c) => (
                  <li key={c.id}>
                    <Link to={`/calls/${c.id}`} className="font-medium underline">
                      {formatDateTime(c.started_at ?? c.ended_at)}
                    </Link>{" "}
                    {c.outcome ? `· ${label(c.outcome)}` : ""}
                    {c.summary ? <p className="text-slate-600">{c.summary}</p> : null}
                    {c.next_step ? <p className="text-slate-500">Next step: {c.next_step}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>
          {prep.known_requirements.length || prep.known_objections.length ? (
            <Card title="Known from earlier calls">
              <ul className="space-y-1 text-sm">
                {[...prep.known_requirements, ...prep.known_objections].map((k, i) => (
                  <li key={i}>
                    <span className="text-xs text-slate-500">{label(k.kind)}:</span> {k.text}
                  </li>
                ))}
              </ul>
            </Card>
          ) : null}
          <Card title="Open action items">
            {prep.open_action_items.length === 0 ? (
              <p className="text-sm text-slate-500">None.</p>
            ) : (
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {prep.open_action_items.map((a) => (
                  <li key={a.id}>{a.title}</li>
                ))}
              </ul>
            )}
          </Card>
          <Card title="Recent notes">
            {prep.recent_notes.length === 0 ? (
              <p className="text-sm text-slate-500">None.</p>
            ) : (
              <ul className="space-y-2 text-sm">
                {prep.recent_notes.map((n) => (
                  <li key={n.id} className="whitespace-pre-line text-slate-700">
                    {n.body}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function SuggestionPanel({
  suggestion,
  onUse,
  onDismiss,
}: {
  suggestion: AgendaSuggestion;
  onUse: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="mb-4 space-y-3 rounded-md border border-amber-200 bg-amber-50 p-3" aria-label="AI suggestion">
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-600">Customer facts (from your records)</p>
        {suggestion.customer_facts.length === 0 ? (
          <p className="text-sm text-slate-600">No recorded facts about this customer.</p>
        ) : (
          <ul className="list-disc pl-5 text-sm text-slate-800">
            {suggestion.customer_facts.map((f, i) => (
              <li key={i}>{f.text}</li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-amber-800">
          AI suggested agenda {suggestion.provider === "mock" ? "(offline mock provider)" : ""}
        </p>
        <ol className="list-decimal pl-5 text-sm text-slate-800">
          {suggestion.ai_suggested_agenda.map((a, i) => (
            <li key={i}>
              <span className="font-medium">{a.title}</span>
              {a.question ? <span className="text-slate-600"> — {a.question}</span> : null}
            </li>
          ))}
        </ol>
        {suggestion.ai_open_questions.length ? (
          <p className="mt-1 text-xs text-slate-600">Open questions: {suggestion.ai_open_questions.join(" · ")}</p>
        ) : null}
      </div>
      <div className="flex gap-2">
        <Button onClick={onUse}>Use this agenda</Button>
        <Button variant="secondary" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>
    </div>
  );
}
