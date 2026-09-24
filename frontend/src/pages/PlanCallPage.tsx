import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { Card, PageHeader, QueryState, SelectField, TextArea } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import { crm, localInputToIso } from "../lib/crm";
import { errorMessage } from "../lib/errors";

/** Plan a call for a contact: objective, desired outcome, optional schedule. */
export function PlanCallPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const contact = useQuery({ queryKey: ["contact", id], queryFn: () => crm.getContact(id) });
  const [objective, setObjective] = useState("");
  const [desired, setDesired] = useState("");
  const [when, setWhen] = useState("");
  const [params] = useSearchParams();
  const teams = params.get("channel") === "TEAMS";
  const [meetingUrl, setMeetingUrl] = useState("");
  const [language, setLanguage] = useState<"en-IN" | "en-US" | "hi-IN" | "de-DE">("en-IN");
  const create = useMutation({
    mutationFn: () =>
      crm.createCall({
        contact_id: id,
        objective: objective.trim() || undefined,
        desired_outcome: desired.trim() || undefined,
        scheduled_at: localInputToIso(when),
        language,
        ...(teams ? { channel: "TEAMS" as const, meeting_url: meetingUrl.trim() } : {}),
      }),
    onSuccess: (call) => {
      queryClient.invalidateQueries({ queryKey: ["calls"] });
      queryClient.invalidateQueries({ queryKey: ["timeline", id] });
      navigate(`/calls/${call.id}/prepare`);
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <QueryState isPending={contact.isPending} error={contact.error}>
      <div className="mx-auto max-w-2xl">
        <PageHeader title={`Plan a ${teams ? "Teams call" : "call"} with ${contact.data?.name ?? ""}`} />
        <Card>
          <form className="space-y-4" onSubmit={onSubmit}>
            {create.error ? <Alert>{errorMessage(create.error)}</Alert> : null}
            <TextArea
              label="Call objective"
              placeholder="e.g. Understand whether they need inventory automation"
              value={objective}
              maxLength={2000}
              onChange={(e) => setObjective(e.target.value)}
            />
            <TextArea
              label="Desired outcome"
              placeholder="e.g. Demo booked for next week"
              value={desired}
              maxLength={2000}
              onChange={(e) => setDesired(e.target.value)}
            />
            {teams ? (
              <TextField
                label="Teams meeting link"
                placeholder="https://teams.microsoft.com/l/meetup-join/..."
                value={meetingUrl}
                required
                maxLength={2000}
                hint="The copilot bot joins this meeting listen-only. Participants see it in the meeting."
                onChange={(e) => setMeetingUrl(e.target.value)}
              />
            ) : null}
            <SelectField
              label="Conversation language (live transcription)"
              value={language}
              onChange={(e) => setLanguage(e.target.value as typeof language)}
              options={[
                { value: "en-IN", label: "English (India)" },
                { value: "en-US", label: "English (US)" },
                { value: "hi-IN", label: "Hindi (India)" },
                { value: "de-DE", label: "German" },
              ]}
            />
            <TextField label="Scheduled for (optional)" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? "Saving…" : "Continue to agenda"}
            </Button>
          </form>
        </Card>
      </div>
    </QueryState>
  );
}
