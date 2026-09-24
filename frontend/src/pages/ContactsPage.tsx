import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { Badge, PageHeader, Pagination, QueryState } from "../components/common";
import { Button } from "../components/ui";
import { CONTACT_STATUSES, crm, label } from "../lib/crm";
import { memberName, useMembers } from "../lib/members";

const LIMIT = 25;

export function ContactsPage() {
  const navigate = useNavigate();
  const members = useMembers();
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);

  const contacts = useQuery({
    queryKey: ["contacts", { search, status, offset }],
    queryFn: () => crm.listContacts({ q: search, status, limit: LIMIT, offset }),
    placeholderData: keepPreviousData,
  });

  return (
    <div>
      <PageHeader
        title="Contacts"
        actions={<Button onClick={() => navigate("/contacts/new")}>Add contact</Button>}
      />
      <form
        className="mb-4 flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          setOffset(0);
          setSearch(q.trim());
        }}
      >
        <input
          aria-label="Search contacts"
          placeholder="Search name, company, phone or email"
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          aria-label="Filter by status"
          className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
          value={status}
          onChange={(e) => {
            setOffset(0);
            setStatus(e.target.value);
          }}
        >
          <option value="">All statuses</option>
          {CONTACT_STATUSES.map((s) => (
            <option key={s} value={s}>
              {label(s)}
            </option>
          ))}
        </select>
        <Button type="submit" variant="secondary">
          Search
        </Button>
      </form>

      <QueryState
        isPending={contacts.isPending}
        error={contacts.error}
        empty={contacts.data?.items.length === 0}
        emptyText={search || status ? "No contacts match your search." : "No contacts yet. Add your first customer."}
      >
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
          {contacts.data?.items.map((c) => (
            <li key={c.id}>
              <Link
                to={`/contacts/${c.id}`}
                className="flex flex-col gap-1 px-4 py-3 hover:bg-slate-50 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium text-slate-900">{c.name}</p>
                  <p className="truncate text-sm text-slate-500">
                    {[c.organization, c.phone, c.email].filter(Boolean).join(" · ") || "No details"}
                  </p>
                </div>
                <div className="flex items-center gap-3 text-xs text-slate-500">
                  {c.tags.slice(0, 3).map((t) => (
                    <span key={t} className="rounded bg-slate-100 px-1.5 py-0.5">
                      {t}
                    </span>
                  ))}
                  <span>{memberName(members.data?.items, c.owner_user_id)}</span>
                  <Badge value={c.status} />
                </div>
              </Link>
            </li>
          ))}
        </ul>
        {contacts.data ? (
          <Pagination
            total={contacts.data.total}
            limit={LIMIT}
            offset={offset}
            onChange={setOffset}
          />
        ) : null}
      </QueryState>
    </div>
  );
}
