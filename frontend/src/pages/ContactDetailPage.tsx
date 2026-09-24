import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ActionItemList, AddActionItemForm } from "../components/ActionItemList";
import { Badge, Card, QueryState } from "../components/common";
import { Timeline } from "../components/Timeline";
import { Alert, Button } from "../components/ui";
import { useAuth } from "../auth/context";
import { crm, formatDateTime, formatDuration, label } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { memberName, useMembers } from "../lib/members";

type Tab = "timeline" | "notes" | "calls" | "tasks";

export function ContactDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const members = useMembers();
  const { session } = useAuth();
  const [tab, setTab] = useState<Tab>("timeline");
  const contact = useQuery({ queryKey: ["contact", id], queryFn: () => crm.getContact(id) });

  const remove = useMutation({
    mutationFn: () => crm.deleteContact(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["contacts"] });
      navigate("/contacts", { replace: true });
    },
  });
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <QueryState isPending={contact.isPending} error={contact.error}>
      {contact.data ? (
        <div className="space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-semibold text-slate-900">{contact.data.name}</h1>
                <Badge value={contact.data.status} />
              </div>
              <p className="text-sm text-slate-600">
                {[contact.data.designation, contact.data.organization].filter(Boolean).join(", ") || "—"}
              </p>
              <p className="text-sm text-slate-500">
                {[contact.data.phone, contact.data.email].filter(Boolean).join(" · ")}
              </p>
              <p className="text-xs text-slate-500">
                Owner: {memberName(members.data?.items, contact.data.owner_user_id)} · Source:{" "}
                {label(contact.data.source)}
                {contact.data.tags.length ? ` · Tags: ${contact.data.tags.join(", ")}` : ""}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => navigate(`/contacts/${id}/prepare`)}>Prepare call</Button>
              <Button variant="secondary" onClick={() => navigate(`/contacts/${id}/edit`)}>
                Edit
              </Button>
              {confirmDelete ? (
                <>
                  <Button variant="secondary" onClick={() => remove.mutate()} disabled={remove.isPending}>
                    Confirm delete
                  </Button>
                  <Button variant="secondary" onClick={() => setConfirmDelete(false)}>
                    Keep
                  </Button>
                </>
              ) : (
                <Button variant="secondary" onClick={() => setConfirmDelete(true)}>
                  Delete
                </Button>
              )}
            </div>
          </div>
          {confirmDelete ? (
            <Alert>
              Deleting removes this contact with all notes, calls, transcripts and tasks. This cannot be
              undone.
            </Alert>
          ) : null}
          {remove.error ? <Alert>{errorMessage(remove.error)}</Alert> : null}
          {contact.data.notes ? (
            <Card title="About">
              <p className="whitespace-pre-line text-sm text-slate-700">{contact.data.notes}</p>
            </Card>
          ) : null}

          <nav className="flex gap-1 border-b border-slate-200" aria-label="Contact sections">
            {(["timeline", "notes", "calls", "tasks"] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                aria-current={tab === t ? "page" : undefined}
                className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                  tab === t ? "border-slate-900 font-medium text-slate-900" : "border-transparent text-slate-500"
                }`}
              >
                {label(t)}
              </button>
            ))}
          </nav>
          {tab === "timeline" ? <Timeline contactId={id} /> : null}
          {tab === "notes" ? <NotesTab contactId={id} myId={session?.user.id} /> : null}
          {tab === "calls" ? <CallsTab contactId={id} /> : null}
          {tab === "tasks" ? <TasksTab contactId={id} /> : null}
        </div>
      ) : null}
    </QueryState>
  );
}

function NotesTab({ contactId, myId }: { contactId: string; myId?: string }) {
  const queryClient = useQueryClient();
  const members = useMembers();
  const [body, setBody] = useState("");
  const notes = useQuery({ queryKey: ["notes", contactId], queryFn: () => crm.listNotes(contactId) });
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["notes", contactId] });
    queryClient.invalidateQueries({ queryKey: ["timeline", contactId] });
  };
  const add = useMutation({
    mutationFn: () => crm.addNote(contactId, body.trim()),
    onSuccess: () => {
      setBody("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: (noteId: string) => crm.deleteNote(contactId, noteId), onSuccess: refresh });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (body.trim()) add.mutate();
  }
  return (
    <div className="space-y-3">
      <form onSubmit={onSubmit} className="space-y-2">
        <textarea
          aria-label="New note"
          rows={3}
          maxLength={10000}
          className="block w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="Write a note…"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <Button type="submit" disabled={!body.trim() || add.isPending}>
          Add note
        </Button>
        {add.error || remove.error ? <Alert>{errorMessage(add.error ?? remove.error)}</Alert> : null}
      </form>
      <QueryState isPending={notes.isPending} error={notes.error} empty={notes.data?.items.length === 0} emptyText="No notes yet.">
        <ul className="space-y-2">
          {notes.data?.items.map((n) => (
            <li key={n.id} className="rounded-md border border-slate-200 bg-white p-3">
              <p className="whitespace-pre-line text-sm text-slate-900">{n.body}</p>
              <div className="mt-1 flex items-center justify-between text-xs text-slate-500">
                <span>
                  {memberName(members.data?.items, n.author_user_id)} · {formatDateTime(n.created_at)}
                </span>
                {n.author_user_id === myId ? (
                  <button className="underline" onClick={() => remove.mutate(n.id)}>
                    Delete
                  </button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      </QueryState>
    </div>
  );
}

function CallsTab({ contactId }: { contactId: string }) {
  const calls = useQuery({
    queryKey: ["calls", { contactId }],
    queryFn: () => crm.listCalls({ contact_id: contactId, limit: 50 }),
  });
  return (
    <QueryState isPending={calls.isPending} error={calls.error} empty={calls.data?.items.length === 0} emptyText="No calls yet. Use “Prepare call” to plan one.">
      <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
        {calls.data?.items.map((c) => (
          <li key={c.id}>
            <Link to={`/calls/${c.id}`} className="flex items-center justify-between px-4 py-3 hover:bg-slate-50">
              <div className="min-w-0">
                <p className="truncate text-sm text-slate-900">{c.objective || "No objective"}</p>
                <p className="text-xs text-slate-500">
                  {formatDateTime(c.started_at ?? c.scheduled_at ?? c.created_at)} · {formatDuration(c.duration_seconds)}
                  {c.outcome ? ` · ${label(c.outcome)}` : ""}
                </p>
              </div>
              <Badge value={c.status} />
            </Link>
          </li>
        ))}
      </ul>
    </QueryState>
  );
}

function TasksTab({ contactId }: { contactId: string }) {
  const items = useQuery({
    queryKey: ["action-items", { contactId }],
    queryFn: () => crm.listActionItems({ contact_id: contactId, limit: 100 }),
  });
  return (
    <div className="space-y-3">
      <AddActionItemForm contactId={contactId} />
      <QueryState isPending={items.isPending} error={items.error}>
        <ActionItemList items={items.data?.items ?? []} />
      </QueryState>
    </div>
  );
}
