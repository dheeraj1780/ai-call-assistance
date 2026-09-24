import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { Badge, PageHeader, Pagination, QueryState } from "../components/common";
import { crm, formatDateTime, formatDuration, label } from "../lib/crm";
import { memberName, useMembers } from "../lib/members";

const LIMIT = 25;
const STATUSES = ["", "PLANNED", "INITIATED", "ACTIVE", "COMPLETED", "NO_ANSWER", "CANCELLED", "FAILED"];

function dayStartIso(value: string, addDays = 0): string | undefined {
  if (!value) return undefined;
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return undefined;
  d.setDate(d.getDate() + addDays);
  return d.toISOString();
}

export function CallHistoryPage() {
  const members = useMembers();
  const [status, setStatus] = useState("");
  const [userId, setUserId] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const calls = useQuery({
    queryKey: ["calls", "history", { status, userId, from, to, offset }],
    queryFn: () =>
      crm.listCalls({
        status,
        user_id: userId,
        from: dayStartIso(from),
        to: dayStartIso(to, 1),
        limit: LIMIT,
        offset,
      }),
    placeholderData: keepPreviousData,
  });
  const reset = (fn: () => void) => {
    setOffset(0);
    fn();
  };

  return (
    <div>
      <PageHeader title="Call history" />
      <div className="mb-4 grid gap-2 sm:grid-cols-4">
        <select aria-label="Status" className="rounded-md border border-slate-300 bg-white px-2 py-2 text-sm" value={status} onChange={(e) => reset(() => setStatus(e.target.value))}>
          {STATUSES.map((s) => (
            <option key={s || "all"} value={s}>
              {s ? label(s) : "All statuses"}
            </option>
          ))}
        </select>
        <select aria-label="Salesperson" className="rounded-md border border-slate-300 bg-white px-2 py-2 text-sm" value={userId} onChange={(e) => reset(() => setUserId(e.target.value))}>
          <option value="">Everyone</option>
          {members.data?.items.map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.full_name}
            </option>
          ))}
        </select>
        <input aria-label="From date" type="date" className="rounded-md border border-slate-300 px-2 py-2 text-sm" value={from} onChange={(e) => reset(() => setFrom(e.target.value))} />
        <input aria-label="To date" type="date" className="rounded-md border border-slate-300 px-2 py-2 text-sm" value={to} onChange={(e) => reset(() => setTo(e.target.value))} />
      </div>
      <QueryState isPending={calls.isPending} error={calls.error} empty={calls.data?.items.length === 0} emptyText="No calls match these filters.">
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
          {calls.data?.items.map((c) => (
            <li key={c.id}>
              <Link to={`/calls/${c.id}`} className="flex flex-col gap-1 px-4 py-3 hover:bg-slate-50 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-900">{c.contact.name}</p>
                  <p className="truncate text-xs text-slate-500">
                    {formatDateTime(c.started_at ?? c.scheduled_at ?? c.created_at)} · {formatDuration(c.duration_seconds)} ·{" "}
                    {memberName(members.data?.items, c.user_id)}
                    {c.objective ? ` · ${c.objective}` : ""}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  {c.outcome ? <span className="text-xs text-slate-500">{label(c.outcome)}</span> : null}
                  <Badge value={c.status} />
                </div>
              </Link>
            </li>
          ))}
        </ul>
        {calls.data ? <Pagination total={calls.data.total} limit={LIMIT} offset={offset} onChange={setOffset} /> : null}
      </QueryState>
    </div>
  );
}
