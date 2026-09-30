import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { CHANNEL_NAME, LANGUAGES, crm, localInputToIso, type Call, type CallPlanEdit } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { parseMeetingCode } from "../lib/meet/meetingCode";
import { SelectField, TextArea } from "./common";
import { Alert, Button, TextField } from "./ui";

type Language = (typeof LANGUAGES)[number]["value"];

/** ISO timestamp -> value for <input type="datetime-local"> in the user's timezone. */
function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Edit a PLANNED call before it starts: objective, desired outcome, schedule, meeting link and
 * language. The server re-validates the meeting link for the call's channel (the copilot must be
 * able to join it). After the call started these fields are fixed and this form is not offered.
 */
export function CallDetailsForm({ call, onSaved, onCancel }: { call: Call; onSaved: (c: Call) => void; onCancel: () => void }) {
  const queryClient = useQueryClient();
  const channel = call.channel ?? "PHONE";
  const [objective, setObjective] = useState(call.objective ?? "");
  const [desired, setDesired] = useState(call.desired_outcome ?? "");
  const [when, setWhen] = useState(isoToLocalInput(call.scheduled_at));
  const [url, setUrl] = useState(call.meeting_url ?? "");
  const [language, setLanguage] = useState<Language>((call.language as Language) ?? "en-IN");
  const save = useMutation({
    mutationFn: () => {
      const body: CallPlanEdit = {
        objective: objective.trim() || null,
        desired_outcome: desired.trim() || null,
        scheduled_at: localInputToIso(when) ?? null,
        language,
      };
      if (channel !== "PHONE") body.meeting_url = url.trim();
      return crm.updateCall(call.id, body);
    },
    onSuccess: (updated) => {
      queryClient.invalidateQueries({ queryKey: ["prep", call.id] });
      queryClient.invalidateQueries({ queryKey: ["call", call.id] });
      queryClient.invalidateQueries({ queryKey: ["calls"] });
      onSaved(updated);
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    save.mutate();
  }
  return (
    <form className="space-y-3" onSubmit={onSubmit} aria-label="Edit call details">
      {save.error ? <Alert>{errorMessage(save.error)}</Alert> : null}
      <p className="text-xs text-slate-500">Channel: {CHANNEL_NAME[channel]} (fixed; plan a new call to use another channel)</p>
      <TextArea label="Call objective" value={objective} maxLength={2000} onChange={(e) => setObjective(e.target.value)} />
      <TextArea label="Desired outcome" value={desired} maxLength={2000} onChange={(e) => setDesired(e.target.value)} />
      {channel !== "PHONE" ? (
        <TextField
          label={channel === "TEAMS" ? "Teams meeting link" : "Google Meet link"}
          value={url}
          required
          maxLength={2000}
          placeholder={channel === "TEAMS" ? "https://teams.microsoft.com/l/meetup-join/..." : "https://meet.google.com/abc-defg-hij"}
          hint={
            channel === "GOOGLE_MEET" && url && !parseMeetingCode(url)
              ? "Not a Google Meet link or meeting code."
              : "Checked again when you save; it cannot be changed once the call has started."
          }
          onChange={(e) => setUrl(e.target.value)}
        />
      ) : null}
      <SelectField
        label="Conversation language (live transcription)"
        value={language}
        onChange={(e) => setLanguage(e.target.value as Language)}
        options={LANGUAGES.map((l) => ({ value: l.value, label: l.label }))}
      />
      <TextField label="Scheduled for (optional)" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save changes"}
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
