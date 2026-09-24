import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { PageHeader, QueryState } from "../components/common";
import { CHANNEL_LABEL, conversationsApi } from "../lib/conversations";
import { formatDateTime } from "../lib/crm";

type Filter = "all" | "UNMATCHED";

/** Unified inbox across WhatsApp and Teams. Unmatched conversations need a human to link
 * them to a contact (the system never guesses). */
export function ConversationsPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const list = useQuery({
    queryKey: ["conversations", filter],
    queryFn: () => conversationsApi.list(filter === "all" ? {} : { match_status: filter }),
    refetchInterval: 10_000,
  });
  return (
    <div className="space-y-4">
      <PageHeader title="Conversations" />
      <div className="flex gap-1" role="group" aria-label="Filter conversations">
        {(["all", "UNMATCHED"] as Filter[]).map((f) => (
          <button
            key={f}
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className={`rounded-full border px-3 py-1 text-xs ${
              filter === f ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 text-slate-600"
            }`}
          >
            {f === "all" ? "All" : "Needs linking"}
          </button>
        ))}
      </div>
      <QueryState
        isPending={list.isPending}
        error={list.error}
        empty={list.data?.length === 0}
        emptyText="No conversations yet. Messages from connected channels appear here."
      >
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
          {list.data?.map((c) => (
            <li key={c.id}>
              <Link to={`/conversations/${c.id}`} className="flex items-start justify-between gap-3 px-4 py-3 hover:bg-slate-50">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {c.contact?.name ?? c.display_name ?? "Unknown sender"}
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs font-normal text-slate-600">
                      {CHANNEL_LABEL[c.channel] ?? c.channel}
                    </span>
                    {c.match_status !== "MATCHED" ? (
                      <span className="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs font-normal text-amber-800">
                        {c.match_status === "AMBIGUOUS" ? "Several possible contacts" : "Not linked"}
                      </span>
                    ) : null}
                    {c.mock ? <span className="ml-1 text-xs text-slate-400">(mock)</span> : null}
                  </p>
                  {c.preview ? <p className="truncate text-sm text-slate-500">{c.preview}</p> : null}
                </div>
                <span className="shrink-0 text-xs text-slate-400">{formatDateTime(c.last_message_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      </QueryState>
    </div>
  );
}
