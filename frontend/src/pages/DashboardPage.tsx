import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { useAuth } from "../auth/context";
import { Card, QueryState } from "../components/common";
import { apiFetch } from "../lib/api";
import { formatDateTime, label, type ActionItem, type Call } from "../lib/crm";

interface Dashboard {
  calls_today: number;
  calls_this_week: number;
  open_action_items: number;
  upcoming_follow_ups: ActionItem[];
  recent_customers: { id: string; name: string; organization: string | null }[];
  calls_needing_follow_up: Call[];
  recent_activity: { id: string; summary: string; occurred_at: string; contact_id: string; contact_name: string; event_type: string }[];
}

const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Kolkata";

export function DashboardPage() {
  const { session } = useAuth();
  const data = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => apiFetch<Dashboard>(`/api/v1/dashboard?tz=${encodeURIComponent(TZ)}`),
  });
  const d = data.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Hello, {session?.user.full_name.split(" ")[0]}</h1>
          <p className="text-sm text-slate-500">{session?.company.name}</p>
        </div>
        <Link to="/contacts" className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white">
          Prepare a call
        </Link>
      </div>
      <QueryState isPending={data.isPending} error={data.error}>
        {d ? (
          <>
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Calls today" value={d.calls_today} />
              <Stat label="This week" value={d.calls_this_week} />
              <Stat label="Open tasks" value={d.open_action_items} to="/action-items" />
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <Card title="Calls needing follow-up">
                {d.calls_needing_follow_up.length === 0 ? (
                  <p className="text-sm text-slate-500">Nothing pending.</p>
                ) : (
                  <ul className="space-y-2 text-sm">
                    {d.calls_needing_follow_up.map((c) => (
                      <li key={c.id} className="flex justify-between gap-2">
                        <Link className="underline" to={`/calls/${c.id}`}>
                          {c.contact.name}
                        </Link>
                        <span className="text-slate-500">{formatDateTime(c.ended_at)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Upcoming follow-ups (7 days)">
                {d.upcoming_follow_ups.length === 0 ? (
                  <p className="text-sm text-slate-500">No follow-ups due.</p>
                ) : (
                  <ul className="space-y-2 text-sm">
                    {d.upcoming_follow_ups.map((i) => (
                      <li key={i.id} className="flex justify-between gap-2">
                        <span className="truncate">
                          {i.title}
                          {i.contact ? <span className="text-slate-500"> · {i.contact.name}</span> : null}
                        </span>
                        <span className="shrink-0 text-slate-500">{formatDateTime(i.due_at)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Recent customers">
                {d.recent_customers.length === 0 ? (
                  <p className="text-sm text-slate-500">
                    No customers yet. <Link className="underline" to="/contacts/new">Add one</Link>.
                  </p>
                ) : (
                  <ul className="space-y-1 text-sm">
                    {d.recent_customers.map((c) => (
                      <li key={c.id}>
                        <Link className="underline" to={`/contacts/${c.id}`}>
                          {c.name}
                        </Link>
                        {c.organization ? <span className="text-slate-500"> · {c.organization}</span> : null}
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
              <Card title="Recent activity">
                {d.recent_activity.length === 0 ? (
                  <p className="text-sm text-slate-500">No activity yet.</p>
                ) : (
                  <ul className="space-y-2 text-sm">
                    {d.recent_activity.map((e) => (
                      <li key={e.id}>
                        <p className="text-xs text-slate-500">
                          {formatDateTime(e.occurred_at)} · {label(e.event_type)} ·{" "}
                          <Link className="underline" to={`/contacts/${e.contact_id}`}>
                            {e.contact_name}
                          </Link>
                        </p>
                        <p className="truncate text-slate-900">{e.summary}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>
          </>
        ) : null}
      </QueryState>
    </div>
  );
}

function Stat({ label: text, value, to }: { label: string; value: number; to?: string }) {
  const body = (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <p className="text-xs text-slate-500">{text}</p>
      <p className="text-2xl font-semibold tabular-nums text-slate-900">{value}</p>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}
