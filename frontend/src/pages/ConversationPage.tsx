import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router";

import { QueryState } from "../components/common";
import { Alert, Button } from "../components/ui";
import {
  CHANNEL_LABEL,
  conversationsApi,
  newClientMessageId,
  type Conversation,
  type Draft,
} from "../lib/conversations";
import { crm, formatDateTime, label } from "../lib/crm";
import { errorMessage } from "../lib/errors";

/** One customer conversation on any messaging channel. AI suggestions are drafts only:
 * the salesperson reviews, optionally edits, and explicitly presses Send. */
export function ConversationPage() {
  const { id = "" } = useParams();
  const conv = useQuery({
    queryKey: ["conversation", id],
    queryFn: () => conversationsApi.get(id),
    refetchInterval: 5_000,
  });
  return (
    <QueryState isPending={conv.isPending} error={conv.error}>
      {conv.data ? <ConversationView conversation={conv.data} /> : null}
    </QueryState>
  );
}

function ConversationView({ conversation }: { conversation: Conversation }) {
  const { session, messages, drafts, notes } = conversation;
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "end" });
  }, [messages.length]);
  const suggestion = drafts.find((d) => d.status === "SUGGESTED");
  return (
    <div className="grid gap-4 lg:grid-cols-12">
      <div className="space-y-3 lg:col-span-8">
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Customer conversation</p>
          <p className="text-sm text-slate-600">
            Channel: <span className="font-medium text-slate-900">{CHANNEL_LABEL[session.channel] ?? session.channel}</span>
            {session.mock ? <span className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800">Mock</span> : null}
          </p>
          <p className="text-sm text-slate-600">
            Customer:{" "}
            {session.contact ? (
              <Link className="font-medium text-slate-900 underline" to={`/contacts/${session.contact.id}`}>
                {session.contact.name}
                {session.contact.organization ? ` (${session.contact.organization})` : ""}
              </Link>
            ) : (
              <span className="font-medium text-slate-900">{session.display_name ?? "Unknown sender"}</span>
            )}
          </p>
        </div>
        {session.match_status !== "MATCHED" ? <LinkContact sessionId={session.id} ambiguous={session.match_status === "AMBIGUOUS"} /> : null}
        <section aria-label="Messages" className="max-h-[55vh] space-y-2 overflow-y-auto rounded-lg border border-slate-200 bg-white p-3">
          {messages.length === 0 ? <p className="text-sm text-slate-500">No messages (older messages are removed by the retention policy).</p> : null}
          {messages.map((m) => (
            <div key={m.id} className={`flex ${m.direction === "OUTBOUND" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${
                  m.direction === "OUTBOUND" ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-900"
                }`}
              >
                <p className="text-xs opacity-70">{m.direction === "OUTBOUND" ? "You" : "Customer"}</p>
                <p className="whitespace-pre-line">{m.content}</p>
                <p className="mt-1 text-right text-[10px] opacity-60">
                  {formatDateTime(m.occurred_at)}
                  {m.direction === "OUTBOUND" ? ` · ${label(m.status)}` : ""}
                </p>
              </div>
            </div>
          ))}
          <div ref={endRef} />
        </section>
        <Composer conversation={conversation} suggestion={suggestion} />
      </div>
      <aside className="space-y-3 lg:col-span-4" aria-label="Extracted notes">
        <div className="rounded-lg border border-slate-200 bg-white p-3">
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">AI extracted (unconfirmed)</h2>
          {notes.length === 0 ? <p className="text-sm text-slate-500">Nothing extracted yet.</p> : null}
          <ul className="space-y-1">
            {notes.map((n) => (
              <li key={n.id} className="text-sm">
                <span className="text-xs text-slate-500">{label(n.kind)}:</span> {n.text}
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}

function Composer({ conversation, suggestion }: { conversation: Conversation; suggestion?: Draft }) {
  const queryClient = useQueryClient();
  const id = conversation.session.id;
  const [text, setText] = useState("");
  const [fromDraft, setFromDraft] = useState<string | null>(null);
  // One idempotency key per composed message: a double click or retry sends it once.
  const [clientId, setClientId] = useState(newClientMessageId);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["conversation", id] });
  const send = useMutation({
    mutationFn: () =>
      conversationsApi.send(id, { text: text.trim(), client_message_id: clientId, draft_id: fromDraft ?? undefined }),
    onSuccess: () => {
      setText("");
      setFromDraft(null);
      setClientId(newClientMessageId());
      refresh();
    },
    onError: refresh,
  });
  const discard = useMutation({
    mutationFn: (draftId: string) => conversationsApi.updateDraft(id, draftId, { status: "DISCARDED" }),
    onSuccess: refresh,
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (text.trim()) send.mutate();
  }
  return (
    <div className="space-y-2">
      {suggestion && fromDraft !== suggestion.id ? (
        <article aria-label="AI suggestion" className="rounded-md border border-sky-200 bg-sky-50 p-3">
          <p className="text-xs font-semibold text-slate-600">
            💡 AI suggestion{suggestion.ai_provider === "mock" ? " (mock AI)" : ""} – review before sending
          </p>
          <p className="mt-1 whitespace-pre-line text-sm text-slate-900">“{suggestion.body}”</p>
          {suggestion.warnings.length ? (
            <ul className="mt-1 text-xs text-amber-800">
              {suggestion.warnings.map((w) => (
                <li key={w}>⚠️ {w}</li>
              ))}
            </ul>
          ) : null}
          <div className="mt-2 flex gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                setText(suggestion.body);
                setFromDraft(suggestion.id);
              }}
            >
              Edit
            </Button>
            <Button variant="secondary" onClick={() => discard.mutate(suggestion.id)}>
              Discard
            </Button>
          </div>
        </article>
      ) : null}
      <form onSubmit={onSubmit} className="space-y-2">
        <textarea
          aria-label="Message"
          rows={3}
          maxLength={4000}
          disabled={!conversation.can_send}
          className="block w-full rounded-md border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50"
          placeholder={conversation.can_send ? "Write a reply…" : ""}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        {!conversation.can_send && conversation.send_blocked_reason ? (
          <p className="text-xs text-amber-800">{conversation.send_blocked_reason}</p>
        ) : null}
        <Button type="submit" disabled={!conversation.can_send || !text.trim() || send.isPending}>
          {send.isPending ? "Sending…" : "Send"}
        </Button>
        {send.error || discard.error ? <Alert>{errorMessage(send.error ?? discard.error)}</Alert> : null}
      </form>
    </div>
  );
}

function LinkContact({ sessionId, ambiguous }: { sessionId: string; ambiguous: boolean }) {
  const queryClient = useQueryClient();
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState("");
  const results = useQuery({
    queryKey: ["contacts", "link-search", q],
    queryFn: () => crm.listContacts({ q, limit: 10 }),
    enabled: q.trim().length >= 2,
  });
  const link = useMutation({
    mutationFn: () => conversationsApi.link(sessionId, picked),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversation", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm">
      <p className="font-medium text-amber-900">
        {ambiguous
          ? "Several contacts share this number. Choose the right customer."
          : "This sender is not linked to a contact yet."}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <input
          aria-label="Search contacts"
          className="rounded-md border border-slate-300 px-2 py-1"
          placeholder="Search contacts…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select aria-label="Contact" className="rounded-md border border-slate-300 px-2 py-1" value={picked} onChange={(e) => setPicked(e.target.value)}>
          <option value="">Select…</option>
          {results.data?.items.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
              {c.phone ? ` · ${c.phone}` : ""}
            </option>
          ))}
        </select>
        <Button variant="secondary" disabled={!picked || link.isPending} onClick={() => link.mutate()}>
          Link to contact
        </Button>
        <Link to="/contacts/new" className="text-xs underline">
          Create a new contact
        </Link>
      </div>
      {link.error ? <Alert>{errorMessage(link.error)}</Alert> : null}
    </div>
  );
}
