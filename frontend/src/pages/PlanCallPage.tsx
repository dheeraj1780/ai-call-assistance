import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { Card, PageHeader, QueryState, SelectField, TextArea } from "../components/common";
import { Alert, Button, TextField } from "../components/ui";
import { CHANNEL_NAME, LANGUAGES, crm, localInputToIso, type ChannelKey } from "../lib/crm";
import { conversationsApi } from "../lib/conversations";
import { errorMessage } from "../lib/errors";
import { parseMeetingCode } from "../lib/meet/meetingCode";

const CALL_CAPABILITIES = new Set(["PHONE_CALL", "REAL_TIME_CALL"]);
const CALL_CHANNELS = new Set<string>(["PHONE", "TEAMS", "GOOGLE_MEET"]);

/**
 * Plan a call for a contact: objective, desired outcome, optional schedule, and HOW the call
 * happens. The channel list comes from the server (only channels that are set up for this
 * company and usable for this contact): a normal phone call needs no Microsoft 365 or Google
 * account; Teams and Google Meet appear only when an admin enabled them.
 */
export function PlanCallPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const contact = useQuery({ queryKey: ["contact", id], queryFn: () => crm.getContact(id) });
  const options = useQuery({ queryKey: ["communication", id], queryFn: () => conversationsApi.options(id) });
  const [objective, setObjective] = useState("");
  const [desired, setDesired] = useState("");
  const [when, setWhen] = useState("");
  const [meetingUrl, setMeetingUrl] = useState("");
  const [language, setLanguage] = useState<(typeof LANGUAGES)[number]["value"]>("en-IN");
  const [chosen, setChosen] = useState<ChannelKey | null>(null);

  const channels = (options.data ?? []).filter(
    (a) => CALL_CAPABILITIES.has(a.capability) && CALL_CHANNELS.has(a.channel),
  );
  const usable = channels.filter((a) => a.available);
  const wanted = params.get("channel") as ChannelKey | null;
  const defaultChannel: ChannelKey | null =
    (usable.find((a) => a.channel === wanted)?.channel as ChannelKey | undefined) ??
    (usable.find((a) => a.channel === "PHONE")?.channel as ChannelKey | undefined) ??
    (usable[0]?.channel as ChannelKey | undefined) ??
    null;
  const channel = chosen ?? defaultChannel;

  const isMeeting = channel === "TEAMS" || channel === "GOOGLE_MEET";
  const create = useMutation({
    mutationFn: () =>
      crm.createCall({
        contact_id: id,
        objective: objective.trim() || undefined,
        desired_outcome: desired.trim() || undefined,
        scheduled_at: localInputToIso(when),
        language,
        channel: channel ?? "PHONE",
        ...(isMeeting ? { meeting_url: meetingUrl.trim() } : {}),
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
  const unavailable = channels.filter((a) => !a.available && a.channel === "PHONE");
  return (
    <QueryState isPending={contact.isPending || options.isPending} error={contact.error ?? options.error}>
      <div className="mx-auto max-w-2xl">
        <PageHeader title={`Plan a ${channel ? CHANNEL_NAME[channel].toLowerCase() : "call"} with ${contact.data?.name ?? ""}`} />
        <Card>
          <form className="space-y-4" onSubmit={onSubmit}>
            {create.error ? <Alert>{errorMessage(create.error)}</Alert> : null}
            {channel === null ? (
              <Alert>
                No calling channel is available for this contact.{" "}
                {unavailable[0]?.reason ? `${unavailable[0].reason}. ` : ""}
                An admin can enable phone calling (Plivo) in{" "}
                <Link className="underline" to="/settings/integrations">
                  Settings → Integrations
                </Link>
                . A Microsoft 365 or Google account is not required for phone calls.
              </Alert>
            ) : (
              <SelectField
                label="How will you talk?"
                value={channel}
                onChange={(e) => {
                  setChosen(e.target.value as ChannelKey);
                  setMeetingUrl("");
                }}
                options={usable.map((a) => ({ value: a.channel, label: CHANNEL_NAME[a.channel as ChannelKey] }))}
              />
            )}
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
            {channel === "TEAMS" ? (
              <TextField
                label="Teams meeting link"
                placeholder="https://teams.microsoft.com/l/meetup-join/..."
                value={meetingUrl}
                required
                maxLength={2000}
                hint="The copilot bot joins this meeting listen-only. Participants see it in the meeting. You can correct the link before starting."
                onChange={(e) => setMeetingUrl(e.target.value)}
              />
            ) : null}
            {channel === "GOOGLE_MEET" ? (
              <TextField
                label="Google Meet link"
                placeholder="https://meet.google.com/abc-defg-hij"
                value={meetingUrl}
                required
                maxLength={2000}
                hint={
                  meetingUrl && !parseMeetingCode(meetingUrl)
                    ? "Not a Google Meet link or meeting code."
                    : "You join the meeting yourself; the copilot then receives its audio listen-only (Meet Media API)."
                }
                onChange={(e) => setMeetingUrl(e.target.value)}
              />
            ) : null}
            <SelectField
              label="Conversation language (live transcription)"
              value={language}
              onChange={(e) => setLanguage(e.target.value as typeof language)}
              options={LANGUAGES.map((l) => ({ value: l.value, label: l.label }))}
            />
            <TextField label="Scheduled for (optional)" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
            <Button type="submit" disabled={create.isPending || channel === null}>
              {create.isPending ? "Saving…" : "Continue to agenda"}
            </Button>
          </form>
        </Card>
      </div>
    </QueryState>
  );
}
