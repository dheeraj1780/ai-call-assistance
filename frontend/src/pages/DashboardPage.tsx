import { useQuery } from "@tanstack/react-query";

import { useAuth } from "../auth/context";
import { Alert } from "../components/ui";
import { apiFetch } from "../lib/api";
import { errorMessage } from "../lib/errors";
import type { Company } from "../lib/types";

export function DashboardPage() {
  const { session } = useAuth();
  const company = useQuery({
    queryKey: ["company", "current"],
    queryFn: () => apiFetch<Company>("/api/v1/companies/current"),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">
          Welcome, {session?.user.full_name}
        </h1>
        <p className="text-sm text-slate-500">Signed in as {session?.user.email}</p>
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-900">Company</h2>
        {company.isPending ? (
          <p className="mt-2 text-sm text-slate-500">Loading…</p>
        ) : company.isError ? (
          <div className="mt-2">
            <Alert>{errorMessage(company.error)}</Alert>
          </div>
        ) : (
          <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
            <div>
              <dt className="text-slate-500">Name</dt>
              <dd className="font-medium text-slate-900">{company.data.name}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Your role</dt>
              <dd className="font-medium text-slate-900">{session?.role}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Transcript retention</dt>
              <dd className="font-medium text-slate-900">
                {company.data.transcript_retention_days} days
              </dd>
            </div>
          </dl>
        )}
      </section>
    </div>
  );
}
