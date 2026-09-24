import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { crm, formatDateTime, label, localInputToIso, type ActionItem, type ActionItemKind } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { Badge } from "./common";
import { Alert, Button } from "./ui";
import { memberName, useMembers } from "../lib/members";

export function ActionItemList({ items, showContact = false }: { items: ActionItem[]; showContact?: boolean }) {
  const queryClient = useQueryClient();
  const members = useMembers();
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["action-items"] });
    queryClient.invalidateQueries({ queryKey: ["timeline"] });
  };
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<ActionItem> }) => crm.updateActionItem(id, body),
    onSuccess: refresh,
  });
  const confirm = useMutation({ mutationFn: (id: string) => crm.confirmActionItem(id), onSuccess: refresh });

  if (items.length === 0) return <p className="text-sm text-slate-500">No action items.</p>;
  return (
    <div className="space-y-2">
      {update.error || confirm.error ? <Alert>{errorMessage(update.error ?? confirm.error)}</Alert> : null}
      <ul className="divide-y divide-slate-100">
        {items.map((item) => {
          const done = item.status === "DONE";
          return (
            <li key={item.id} className="flex flex-col gap-2 py-2 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 items-start gap-3">
                <input
                  type="checkbox"
                  aria-label={`Mark "${item.title}" done`}
                  className="mt-1"
                  checked={done}
                  disabled={!item.is_confirmed || update.isPending}
                  onChange={() => update.mutate({ id: item.id, body: { status: done ? "OPEN" : "DONE" } })}
                />
                <div className="min-w-0">
                  <p className={`text-sm ${done ? "text-slate-400 line-through" : "text-slate-900"}`}>{item.title}</p>
                  <p className="text-xs text-slate-500">
                    {label(item.kind)} · due {formatDateTime(item.due_at)} ·{" "}
                    {memberName(members.data?.items, item.assignee_user_id)}
                    {showContact && item.contact ? (
                      <>
                        {" · "}
                        <Link className="underline" to={`/contacts/${item.contact.id}`}>
                          {item.contact.name}
                        </Link>
                      </>
                    ) : null}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {!item.is_confirmed ? (
                  <>
                    <span className="rounded bg-amber-50 px-2 py-0.5 text-xs text-amber-800">AI suggestion</span>
                    <Button variant="secondary" onClick={() => confirm.mutate(item.id)}>
                      Confirm
                    </Button>
                  </>
                ) : (
                  <Badge value={item.status} />
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function AddActionItemForm({ contactId, callId }: { contactId?: string; callId?: string }) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState<ActionItemKind>("TASK");
  const [due, setDue] = useState("");
  const create = useMutation({
    mutationFn: () =>
      crm.createActionItem({
        title: title.trim(),
        kind,
        contact_id: callId ? undefined : contactId,
        call_id: callId,
        due_at: localInputToIso(due),
      }),
    onSuccess: () => {
      setTitle("");
      setDue("");
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
      queryClient.invalidateQueries({ queryKey: ["timeline"] });
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (title.trim()) create.mutate();
  }
  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-2 sm:flex-row">
      <input
        aria-label="New action item"
        placeholder="Add a task, follow-up or appointment"
        className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
        value={title}
        maxLength={300}
        onChange={(e) => setTitle(e.target.value)}
      />
      <select
        aria-label="Kind"
        className="rounded-md border border-slate-300 bg-white px-2 py-2 text-sm"
        value={kind}
        onChange={(e) => setKind(e.target.value as ActionItemKind)}
      >
        <option value="TASK">Task</option>
        <option value="FOLLOW_UP">Follow-up</option>
        <option value="APPOINTMENT">Appointment</option>
      </select>
      <input
        aria-label="Due"
        type="datetime-local"
        className="rounded-md border border-slate-300 px-2 py-2 text-sm"
        value={due}
        onChange={(e) => setDue(e.target.value)}
      />
      <Button type="submit" disabled={!title.trim() || create.isPending}>
        Add
      </Button>
      {create.error ? <Alert>{errorMessage(create.error)}</Alert> : null}
    </form>
  );
}
