import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import { Card, PageHeader, QueryState } from "../components/common";
import { ScheduleEventForm } from "../components/ScheduleEventForm";
import { Alert, Button } from "../components/ui";
import { ApiError } from "../lib/api";
import { calendarApi } from "../lib/calendar";
import { formatDateTime } from "../lib/crm";
import { errorMessage } from "../lib/errors";

const CALLBACK_ERRORS: Record<string, string> = {
  denied: "Google Calendar access was not granted.",
  invalid_state: "The connection link expired or was invalid. Please try again.",
  connect_failed: "Could not connect to Google Calendar. Please try again.",
};

export function CalendarPage() {
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const connection = useQuery({ queryKey: ["calendar", "connection"], queryFn: calendarApi.connection });
  const connected = connection.data?.connected && connection.data.status === "ACTIVE";
  const upcoming = useQuery({
    queryKey: ["calendar", "upcoming"],
    queryFn: () => calendarApi.upcoming(14),
    enabled: Boolean(connected),
    retry: false,
  });
  const connect = useMutation({
    mutationFn: calendarApi.connect,
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
  });
  const disconnect = useMutation({
    mutationFn: calendarApi.disconnect,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["calendar"] }),
  });
  const callbackError = params.get("error");
  const needsReconnect =
    connection.data?.status === "REAUTH_REQUIRED" ||
    (upcoming.error instanceof ApiError && upcoming.error.code === "calendar_reauth_required");

  return (
    <div className="space-y-4">
      <PageHeader title="Calendar" />
      {params.get("connected") ? <p className="text-sm text-emerald-700">Google Calendar connected.</p> : null}
      {callbackError ? <Alert>{CALLBACK_ERRORS[callbackError] ?? "Calendar connection failed."}</Alert> : null}

      <Card title="Google Calendar">
        <QueryState isPending={connection.isPending} error={connection.error}>
          {connection.data?.connected ? (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-slate-700">
                Connected{connection.data.account_email ? ` as ${connection.data.account_email}` : ""}
                {connection.data.provider === "mock" ? " (offline mock calendar)" : ""}
              </p>
              <div className="flex gap-2">
                {needsReconnect ? <Button onClick={() => connect.mutate()}>Reconnect</Button> : null}
                <Button variant="secondary" onClick={() => disconnect.mutate()} disabled={disconnect.isPending}>
                  Disconnect
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-slate-600">
                Connect your calendar to see upcoming meetings and add follow-ups. Events are only created when
                you confirm them.
              </p>
              <Button onClick={() => connect.mutate()} disabled={connect.isPending}>
                Connect Google Calendar
              </Button>
            </div>
          )}
          {needsReconnect ? (
            <div className="mt-2">
              <Alert>Your calendar connection expired. Reconnect to continue.</Alert>
            </div>
          ) : null}
          {connect.error ? <Alert>{errorMessage(connect.error)}</Alert> : null}
        </QueryState>
      </Card>

      {connected ? (
        <>
          <Card title="Schedule a follow-up">
            <ScheduleEventForm defaultTitle="Follow-up call" />
          </Card>
          <Card title="Next 14 days">
            <QueryState
              isPending={upcoming.isPending}
              error={needsReconnect ? null : upcoming.error}
              empty={upcoming.data?.length === 0}
              emptyText="No upcoming events."
            >
              <ul className="divide-y divide-slate-100">
                {upcoming.data?.map((e) => (
                  <li key={e.external_id} className="flex justify-between py-2 text-sm">
                    <span className="text-slate-900">{e.title}</span>
                    <span className="text-slate-500">{formatDateTime(e.starts_at)}</span>
                  </li>
                ))}
              </ul>
            </QueryState>
          </Card>
        </>
      ) : null}
    </div>
  );
}
