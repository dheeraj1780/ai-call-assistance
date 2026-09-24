import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { TIMELINE_CATEGORIES, crm, formatDateTime, label } from "../lib/crm";
import { QueryState } from "./common";
import { Button } from "./ui";

const ICON: Record<string, string> = {
  CALL: "📞",
  NOTE: "📝",
  TASK: "✅",
  FOLLOW_UP: "↩️",
  APPOINTMENT: "📅",
  STATUS_CHANGE: "🔄",
  CONTACT: "👤",
  SUMMARY: "🧾",
  CALENDAR: "📅",
};

export function Timeline({ contactId }: { contactId: string }) {
  const [category, setCategory] = useState<string>("");
  const timeline = useInfiniteQuery({
    queryKey: ["timeline", contactId, category],
    queryFn: ({ pageParam }) =>
      crm.timeline(contactId, { limit: 20, before: pageParam, category: category || undefined }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const events = timeline.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <div>
      <div className="mb-3 flex flex-wrap gap-1" role="group" aria-label="Filter timeline">
        {["", ...TIMELINE_CATEGORIES].map((c) => (
          <button
            key={c || "all"}
            onClick={() => setCategory(c)}
            aria-pressed={category === c}
            className={`rounded-full border px-3 py-1 text-xs ${
              category === c ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 text-slate-600"
            }`}
          >
            {c ? label(c) : "All"}
          </button>
        ))}
      </div>
      <QueryState
        isPending={timeline.isPending}
        error={timeline.error}
        empty={events.length === 0}
        emptyText="No activity yet."
      >
        <ol className="relative space-y-4 border-l border-slate-200 pl-5">
          {events.map((e) => (
            <li key={e.id}>
              <span className="absolute -left-2.5 flex h-5 w-5 items-center justify-center rounded-full bg-white text-xs">
                {ICON[e.category] ?? "•"}
              </span>
              <p className="text-xs text-slate-500">
                {formatDateTime(e.occurred_at)} · {label(e.event_type)}
              </p>
              <p className="whitespace-pre-line text-sm text-slate-900">{e.summary}</p>
              {e.call_id ? (
                <Link to={`/calls/${e.call_id}`} className="text-xs text-slate-600 underline">
                  View call
                </Link>
              ) : null}
            </li>
          ))}
        </ol>
        {timeline.hasNextPage ? (
          <div className="mt-4">
            <Button
              variant="secondary"
              disabled={timeline.isFetchingNextPage}
              onClick={() => timeline.fetchNextPage()}
            >
              {timeline.isFetchingNextPage ? "Loading…" : "Load older activity"}
            </Button>
          </div>
        ) : null}
      </QueryState>
    </div>
  );
}
