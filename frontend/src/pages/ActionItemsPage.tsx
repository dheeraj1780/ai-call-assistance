import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ActionItemList, AddActionItemForm } from "../components/ActionItemList";
import { Card, PageHeader, Pagination, QueryState } from "../components/common";
import { useAuth } from "../auth/context";
import { crm } from "../lib/crm";

const LIMIT = 50;
type Filter = "mine" | "open" | "suggested" | "done" | "all";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "mine", label: "Assigned to me" },
  { value: "open", label: "All open" },
  { value: "suggested", label: "AI suggestions" },
  { value: "done", label: "Done" },
  { value: "all", label: "Everything" },
];

export function ActionItemsPage() {
  const { session } = useAuth();
  const [filter, setFilter] = useState<Filter>("mine");
  const [offset, setOffset] = useState(0);
  const params = {
    mine: { assignee_user_id: session?.user.id, status: ["OPEN", "IN_PROGRESS"] },
    open: { status: ["OPEN", "IN_PROGRESS"] },
    suggested: { confirmed: false },
    done: { status: ["DONE"] },
    all: {},
  }[filter];
  const items = useQuery({
    queryKey: ["action-items", { filter, offset }],
    queryFn: () => crm.listActionItems({ ...params, limit: LIMIT, offset }),
  });

  return (
    <div className="space-y-4">
      <PageHeader title="Action items" />
      <Card>
        <AddActionItemForm />
      </Card>
      <div className="flex flex-wrap gap-1" role="group" aria-label="Filter action items">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            aria-pressed={filter === f.value}
            onClick={() => {
              setOffset(0);
              setFilter(f.value);
            }}
            className={`rounded-full border px-3 py-1 text-xs ${
              filter === f.value ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 text-slate-600"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>
      <Card>
        <QueryState isPending={items.isPending} error={items.error}>
          <ActionItemList items={items.data?.items ?? []} showContact />
          {items.data ? (
            <Pagination total={items.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          ) : null}
        </QueryState>
      </Card>
    </div>
  );
}
