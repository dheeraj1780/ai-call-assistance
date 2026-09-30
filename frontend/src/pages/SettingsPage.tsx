import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { useAuth } from "../auth/context";
import { Card, PageHeader, QueryState, TextArea } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import { apiFetch } from "../lib/api";
import { errorMessage } from "../lib/errors";
import type { Company } from "../lib/types";

export function SettingsPage() {
  const { session } = useAuth();
  const isAdmin = session?.role === "OWNER" || session?.role === "ADMIN";
  const company = useQuery({ queryKey: ["company", "current"], queryFn: () => apiFetch<Company>("/api/v1/companies/current") });
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <PageHeader title="Settings" />
      <Card title="Integrations">
        <p className="mb-2 text-sm text-slate-600">Set up phone calling (Plivo). Optional: WhatsApp Business, Microsoft Teams and Google Meet.</p>
        <Link to="/settings/integrations" className="text-sm font-medium text-slate-900 underline">
          Open Settings → Integrations
        </Link>
      </Card>
      <ProfileCard />
      <QueryState isPending={company.isPending} error={company.error}>
        {company.data ? <CompanyCard company={company.data} editable={isAdmin} /> : null}
      </QueryState>
    </div>
  );
}

function ProfileCard() {
  const { session } = useAuth();
  const [name, setName] = useState(session?.user.full_name ?? "");
  const [phone, setPhone] = useState(session?.user.phone ?? "");
  const [saved, setSaved] = useState(false);
  const save = useMutation({
    mutationFn: () => apiFetch("/api/v1/me", { method: "PATCH", body: { full_name: name.trim(), phone: phone.trim() || null } }),
    onSuccess: () => setSaved(true),
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    save.mutate();
  }
  return (
    <Card title="Your profile">
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={onSubmit}>
        <TextField label="Name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        <TextField
          label="Your phone (the calling provider rings this number)"
          value={phone}
          maxLength={32}
          onChange={(e) => setPhone(e.target.value)}
        />
        <div className="flex items-center gap-3 sm:col-span-2">
          <Button type="submit" disabled={save.isPending}>
            Save profile
          </Button>
          {saved ? <span className="text-xs text-emerald-700">Saved</span> : null}
        </div>
        {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
      </form>
    </Card>
  );
}

function CompanyCard({ company, editable }: { company: Company; editable: boolean }) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState({
    name: company.name,
    industry: company.industry ?? "",
    description: company.description ?? "",
    products_services: company.products_services ?? "",
    target_customer: company.target_customer ?? "",
    ai_instructions: company.ai_instructions ?? "",
    transcript_retention_days: String(company.transcript_retention_days),
  });
  const [saved, setSaved] = useState(false);
  const set = (k: keyof typeof values) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [k]: e.target.value }));
  const days = Number(values.transcript_retention_days);
  const daysValid = Number.isInteger(days) && days >= 1 && days <= 365;
  const save = useMutation({
    mutationFn: () =>
      apiFetch<Company>("/api/v1/companies/current", {
        method: "PATCH",
        body: {
          name: values.name.trim(),
          industry: values.industry.trim() || null,
          description: values.description.trim() || null,
          products_services: values.products_services.trim() || null,
          target_customer: values.target_customer.trim() || null,
          ai_instructions: values.ai_instructions.trim() || null,
          transcript_retention_days: days,
        },
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["company", "current"], updated);
      setSaved(true);
    },
  });
  return (
    <Card title="Company">
      <form
        className="grid gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setSaved(false);
          if (daysValid) save.mutate();
        }}
      >
        <fieldset disabled={!editable} className="grid gap-3 sm:grid-cols-2">
          <TextField label="Company name" value={values.name} onChange={set("name")} />
          <TextField label="Industry" value={values.industry} onChange={set("industry")} />
          <div className="sm:col-span-2">
            <TextArea label="What does your company do?" value={values.description} onChange={set("description")} />
          </div>
          <div className="sm:col-span-2">
            <TextArea label="Products and services" value={values.products_services} onChange={set("products_services")} />
          </div>
          <div className="sm:col-span-2">
            <TextArea label="Target customers" value={values.target_customer} onChange={set("target_customer")} />
          </div>
          <div className="sm:col-span-2">
            <TextArea
              label="Instructions for the AI (tone, things to avoid)"
              value={values.ai_instructions}
              onChange={set("ai_instructions")}
            />
          </div>
          <TextField
            label="Keep call transcripts for (days)"
            type="number"
            min={1}
            max={365}
            value={values.transcript_retention_days}
            onChange={set("transcript_retention_days")}
            error={daysValid ? undefined : "Choose between 1 and 365 days."}
            hint="Transcripts are deleted automatically after this period. Audio is never stored."
          />
        </fieldset>
        {editable ? (
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={save.isPending || !daysValid}>
              Save company
            </Button>
            {saved ? <span className="text-xs text-emerald-700">Saved</span> : null}
          </div>
        ) : (
          <p className="text-xs text-slate-500">Only owners and admins can change company settings.</p>
        )}
        {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
      </form>
    </Card>
  );
}
