import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { useAuth } from "../auth/context";
import { Card, PageHeader, QueryState, SelectField } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import { errorMessage } from "../lib/errors";
import {
  STATE_STYLE,
  buildConfigureBody,
  integrationsApi,
  type Integration,
  type IntegrationCapability,
  type IntegrationDetail,
} from "../lib/integrations";
import { label } from "../lib/crm";

const STATUS_TEXT: Record<string, string> = {
  NOT_CONFIGURED: "Not connected",
  CONFIGURED: "Configuration saved – not tested",
  VALIDATED: "Connected (validated)",
  ENABLED: "Connected",
  ERROR: "Error",
};

const TEAMS_MESSAGES: Record<string, string> = {
  connected: "Your Microsoft Teams account is connected.",
  denied: "Microsoft sign-in was cancelled.",
  invalid_state: "The sign-in link expired. Please try again.",
  teams_permissions_missing: "Microsoft did not grant the required permissions.",
  connect_failed: "Microsoft Teams could not be connected. Please try again.",
};

export function IntegrationsPage() {
  const { session } = useAuth();
  const isAdmin = session?.role === "OWNER" || session?.role === "ADMIN";
  const [params] = useSearchParams();
  const list = useQuery({ queryKey: ["integrations"], queryFn: integrationsApi.list });
  const [open, setOpen] = useState<string | null>(null);
  const teamsResult = params.get("teams");
  const consent = params.get("consent");

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <PageHeader
        title="Integrations"
        actions={
          <Link to="/settings" className="text-sm text-slate-600 underline">
            Back to settings
          </Link>
        }
      />
      <p className="text-sm text-slate-600">
        Connect the channels your customers use. Credentials are stored encrypted and are never shown again after saving.
        Nothing is sent to a customer without your explicit action.
      </p>
      {teamsResult ? (
        <div role="status" className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm">
          {TEAMS_MESSAGES[teamsResult] ?? "Microsoft Teams could not be connected."}
        </div>
      ) : null}
      {consent ? (
        <div role="status" className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm">
          {consent === "granted"
            ? "Admin consent was granted. Run Test Connection to verify the calling permissions."
            : "Admin consent was not granted."}
        </div>
      ) : null}
      <QueryState isPending={list.isPending} error={list.error}>
        <div className="space-y-4">
          {list.data?.map((integration) => (
            <IntegrationCard
              key={integration.slug}
              integration={integration}
              isAdmin={isAdmin}
              expanded={open === integration.slug}
              onToggle={() => setOpen(open === integration.slug ? null : integration.slug)}
            />
          ))}
        </div>
      </QueryState>
    </div>
  );
}

function StateBadge({ state }: { state: IntegrationCapability["state"] }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATE_STYLE[state]}`}>{label(state)}</span>;
}

function IntegrationCard({
  integration,
  isAdmin,
  expanded,
  onToggle,
}: {
  integration: Integration;
  isAdmin: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4" aria-label={integration.name}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">{integration.name}</h2>
          <p className="text-sm text-slate-500">{integration.subtitle}</p>
          <p className="mt-1 text-sm">
            Status:{" "}
            <span className={integration.status === "ERROR" ? "text-rose-700" : "text-slate-800"}>
              {STATUS_TEXT[integration.status] ?? integration.status}
            </span>
            {integration.mode === "MOCK" ? (
              <span className="ml-2 rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800">Mock mode</span>
            ) : null}
          </p>
        </div>
        <Button variant="secondary" onClick={onToggle} aria-expanded={expanded}>
          {expanded ? "Close" : isAdmin ? "Configure" : "Details"}
        </Button>
      </div>
      <ul className="mt-3 space-y-1" aria-label={`${integration.name} capabilities`}>
        {integration.capabilities.map((c) => (
          <li key={c.capability} className="flex flex-wrap items-center gap-2 text-sm">
            <span aria-hidden="true">{c.enabled ? "☑" : "☐"}</span>
            <span className={c.available ? "text-slate-900" : "text-slate-400"}>{c.label}</span>
            <StateBadge state={c.state} />
            {!c.available && c.reasons[0] ? <span className="text-xs text-slate-500">{c.reasons[0]}</span> : null}
          </li>
        ))}
      </ul>
      {expanded ? <IntegrationPanel slug={integration.slug} isAdmin={isAdmin} /> : null}
    </section>
  );
}

function IntegrationPanel({ slug, isAdmin }: { slug: string; isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ["integration", slug], queryFn: () => integrationsApi.get(slug) });
  const update = (d: IntegrationDetail) => {
    queryClient.setQueryData(["integration", slug], d);
    queryClient.invalidateQueries({ queryKey: ["integrations"] });
  };
  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      <QueryState isPending={detail.isPending} error={detail.error}>
        {detail.data ? (
          <div className="space-y-4">
            {isAdmin ? (
              <ConfigForm
                // Remount with fresh values whenever the stored configuration changes.
                key={`${detail.data.mode}:${detail.data.fields.map((f) => `${f.key}=${f.value ?? ""}/${f.is_set}`).join("|")}`}
                detail={detail.data}
                onSaved={update}
              />
            ) : null}
            <Requirements detail={detail.data} />
            {isAdmin ? <Actions detail={detail.data} onChange={update} /> : null}
            {slug === "microsoft-teams" && detail.data.mode === "LIVE" ? <TeamsUserConnection isAdmin={isAdmin} /> : null}
            <SetupInfo detail={detail.data} />
            {detail.data.mode === "MOCK" ? <DeveloperControls detail={detail.data} /> : null}
          </div>
        ) : null}
      </QueryState>
    </div>
  );
}

function ConfigForm({ detail, onSaved }: { detail: IntegrationDetail; onSaved: (d: IntegrationDetail) => void }) {
  const initial = () => Object.fromEntries(detail.fields.map((f) => [f.key, f.kind === "secret" ? "" : f.value ?? ""]));
  const [form, setForm] = useState<Record<string, string>>(initial);
  const [mode, setMode] = useState<"LIVE" | "MOCK">(detail.mode);
  const [saved, setSaved] = useState(false);
  const save = useMutation({
    mutationFn: () => integrationsApi.configure(detail.slug, buildConfigureBody(detail.fields, form, mode)),
    onSuccess: (d) => {
      setSaved(true);
      onSaved(d);
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSaved(false);
    save.mutate();
  }
  return (
    <form className="space-y-3" onSubmit={onSubmit} aria-label={`${detail.name} configuration`}>
      <SelectField
        label="Mode"
        value={mode}
        onChange={(e) => setMode(e.target.value as "LIVE" | "MOCK")}
        options={[
          { value: "LIVE", label: "Live (real provider credentials)" },
          ...(detail.mock_allowed ? [{ value: "MOCK", label: "Mock (developer testing, nothing is sent)" }] : []),
        ]}
      />
      {mode === "LIVE" ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {detail.fields.map((f) =>
            f.kind === "select" ? (
              <div key={f.key} className="sm:col-span-2">
                <SelectField
                  label={f.label}
                  value={form[f.key] ?? ""}
                  onChange={(e) => setForm((v) => ({ ...v, [f.key]: e.target.value }))}
                  options={f.options}
                />
                <p className="mt-1 text-xs text-slate-500">{f.help}</p>
              </div>
            ) : (
              <TextField
                key={f.key}
                label={f.label}
                type={f.kind === "secret" ? "password" : "text"}
                autoComplete={f.kind === "secret" ? "new-password" : "off"}
                maxLength={f.max_length}
                value={form[f.key] ?? ""}
                placeholder={f.kind === "secret" && f.is_set ? `${f.display} (stored – type to replace)` : ""}
                hint={f.help}
                onChange={(e) => setForm((v) => ({ ...v, [f.key]: e.target.value }))}
              />
            ),
          )}
        </div>
      ) : (
        <p className="text-sm text-slate-600">
          Mock mode uses built-in simulated providers. No credentials are needed and nothing is sent to real customers.
        </p>
      )}
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save"}
        </Button>
        {saved ? <span className="text-xs text-emerald-700">Saved. Run Test Connection next.</span> : null}
      </div>
      {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
    </form>
  );
}

function Requirements({ detail }: { detail: IntegrationDetail }) {
  const incomplete = detail.requirements.some((r) => !r.ok);
  return (
    <Card title={incomplete ? "Configuration incomplete" : "Configuration check"}>
      <ul className="space-y-1 text-sm">
        {detail.requirements.map((r) => (
          <li key={r.key}>
            <span aria-hidden="true" className={r.ok ? "text-emerald-700" : "text-rose-700"}>
              {r.ok ? "✓" : "✗"}
            </span>{" "}
            {r.label}
            {r.capability ? <span className="text-xs text-slate-400"> · {label(r.capability)}</span> : null}
            {r.scope === "server" ? <span className="text-xs text-slate-400"> · server setting</span> : null}
            {!r.ok && r.hint ? <p className="ml-5 text-xs text-slate-500">{r.hint}</p> : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Actions({ detail, onChange }: { detail: IntegrationDetail; onChange: (d: IntegrationDetail) => void }) {
  const queryClient = useQueryClient();
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const test = useMutation({ mutationFn: () => integrationsApi.test(detail.slug), onSuccess: onChange });
  const toggle = useMutation({
    mutationFn: ({ cap, enable }: { cap: string; enable: boolean }) => integrationsApi.setCapability(detail.slug, cap, enable),
    onSuccess: onChange,
  });
  const disconnect = useMutation({
    mutationFn: () => integrationsApi.disconnect(detail.slug),
    onSuccess: () => {
      setConfirmDisconnect(false);
      queryClient.invalidateQueries({ queryKey: ["integrations"] });
      queryClient.invalidateQueries({ queryKey: ["integration", detail.slug] });
    },
  });
  const error = test.error ?? toggle.error ?? disconnect.error;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" disabled={test.isPending || detail.status === "NOT_CONFIGURED"} onClick={() => test.mutate()}>
          {test.isPending ? "Testing…" : "Test Connection"}
        </Button>
        {confirmDisconnect ? (
          <>
            <Button variant="secondary" onClick={() => disconnect.mutate()} disabled={disconnect.isPending}>
              Confirm disconnect
            </Button>
            <Button variant="secondary" onClick={() => setConfirmDisconnect(false)}>
              Keep
            </Button>
          </>
        ) : (
          <Button variant="secondary" disabled={detail.status === "NOT_CONFIGURED"} onClick={() => setConfirmDisconnect(true)}>
            Disconnect
          </Button>
        )}
      </div>
      {confirmDisconnect ? (
        <Alert>Disconnecting deletes the stored credentials. Past conversations stay in the CRM.</Alert>
      ) : null}
      {detail.last_test ? (
        <Card title={`Last connection test: ${detail.last_test.ok ? "passed" : "failed"}`}>
          {detail.last_test.account_label ? <p className="mb-2 text-sm text-slate-600">Account: {detail.last_test.account_label}</p> : null}
          <ul className="space-y-1 text-sm">
            {detail.last_test.checks.map((c) => (
              <li key={c.key}>
                <span className={c.ok ? "text-emerald-700" : c.required ? "text-rose-700" : "text-amber-700"}>{c.ok ? "✓" : "✗"}</span> {c.label}
                {!c.required ? <span className="text-xs text-slate-400"> (optional)</span> : null}
                {c.detail ? <p className="ml-5 text-xs text-slate-500">{c.detail}</p> : null}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
      <ul className="space-y-2">
        {detail.capabilities
          .filter((c) => c.available)
          .map((c) => {
            const canEnable = c.state === "VALIDATED";
            return (
              <li key={c.capability} className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-100 p-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {c.label} <StateBadge state={c.state} />
                  </p>
                  <p className="text-xs text-slate-500">{c.description}</p>
                  {c.reasons.length && !c.enabled ? <p className="text-xs text-amber-700">{c.reasons.join(" · ")}</p> : null}
                </div>
                <Button
                  variant="secondary"
                  disabled={toggle.isPending || (!c.enabled && !canEnable)}
                  onClick={() => toggle.mutate({ cap: c.capability, enable: !c.enabled })}
                >
                  {c.enabled ? `Disable ${c.label}` : `Enable ${c.label}`}
                </Button>
              </li>
            );
          })}
      </ul>
      {error ? <Alert>{errorMessage(error)}</Alert> : null}
    </div>
  );
}

function TeamsUserConnection({ isAdmin }: { isAdmin: boolean }) {
  const queryClient = useQueryClient();
  const conn = useQuery({ queryKey: ["teams-connection"], queryFn: integrationsApi.teamsConnection });
  const connect = useMutation({
    mutationFn: integrationsApi.teamsConnect,
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  const disconnect = useMutation({
    mutationFn: integrationsApi.teamsDisconnect,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["teams-connection"] }),
  });
  const consent = useMutation({
    mutationFn: integrationsApi.teamsAdminConsentUrl,
    onSuccess: ({ url }) => window.location.assign(url),
  });
  return (
    <Card title="Your Microsoft Teams account">
      <p className="mb-2 text-sm text-slate-600">
        {conn.data?.connected
          ? `Connected as ${conn.data.account_email ?? "your account"}.`
          : "Each salesperson connects their own account to read linked chats and send replies."}
      </p>
      <div className="flex flex-wrap gap-2">
        {conn.data?.connected ? (
          <Button variant="secondary" onClick={() => disconnect.mutate()}>
            Disconnect my Teams account
          </Button>
        ) : (
          <Button onClick={() => connect.mutate()} disabled={connect.isPending}>
            Connect my Teams account
          </Button>
        )}
        {isAdmin ? (
          <Button variant="secondary" onClick={() => consent.mutate()} disabled={consent.isPending}>
            Grant admin consent (calling)
          </Button>
        ) : null}
      </div>
      {connect.error || consent.error || disconnect.error ? (
        <Alert>{errorMessage(connect.error ?? consent.error ?? disconnect.error)}</Alert>
      ) : null}
    </Card>
  );
}

function SetupInfo({ detail }: { detail: IntegrationDetail }) {
  const entries = Object.entries(detail.setup).filter(([, v]) => v);
  if (!entries.length) return null;
  return (
    <Card title="Provider setup values">
      <dl className="space-y-2 text-sm">
        {entries.map(([k, v]) => (
          <div key={k}>
            <dt className="text-xs font-medium text-slate-500">{label(k)}</dt>
            <dd className="break-all font-mono text-xs text-slate-800">{Array.isArray(v) ? v.join(", ") : v}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-slate-500">Step-by-step activation guide: {detail.docs}</p>
    </Card>
  );
}

function DeveloperControls({ detail }: { detail: IntegrationDetail }) {
  const [text, setText] = useState("Hi, can you send the quotation for 100 units?");
  const [phone, setPhone] = useState("+919876543210");
  const [result, setResult] = useState<string | null>(null);
  const messaging = detail.capabilities.find((c) => c.capability === "MESSAGE");
  const kind = detail.slug === "whatsapp" ? "whatsapp-message" : detail.slug === "microsoft-teams" ? "teams-message" : null;
  const simulate = useMutation({
    mutationFn: () =>
      integrationsApi.simulateMessage(kind!, kind === "whatsapp-message" ? { text, phone } : { text }),
    onSuccess: (r) => setResult(`Simulated: ${JSON.stringify(r)}. Open Conversations to see it.`),
  });
  return (
    <Card title="Developer controls (mock mode)">
      {kind && messaging?.enabled ? (
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            setResult(null);
            simulate.mutate();
          }}
        >
          {kind === "whatsapp-message" ? (
            <TextField label="Customer phone" value={phone} onChange={(e) => setPhone(e.target.value)} />
          ) : (
            <p className="text-xs text-slate-500">Arrives in the first mock chat; link it to a contact from the contact page.</p>
          )}
          <TextField label="Customer message" value={text} maxLength={2000} onChange={(e) => setText(e.target.value)} />
          <Button type="submit" disabled={simulate.isPending || !text.trim()}>
            {kind === "whatsapp-message" ? "Simulate WhatsApp Message" : "Simulate Teams Message"}
          </Button>
        </form>
      ) : null}
      <p className="mt-2 text-xs text-slate-500">
        {detail.slug === "plivo" || detail.slug === "microsoft-teams"
          ? "Simulate calls from the live call screen: start a call, then use “Simulate conversation (mock)”."
          : "Enable Messages to simulate incoming messages."}
      </p>
      {result ? <p className="mt-2 text-xs text-emerald-700">{result}</p> : null}
      {simulate.error ? <Alert>{errorMessage(simulate.error)}</Alert> : null}
    </Card>
  );
}
