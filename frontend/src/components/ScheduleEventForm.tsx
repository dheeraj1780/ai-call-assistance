import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { calendarApi, type NewCalendarEvent } from "../lib/calendar";
import { formatDateTime } from "../lib/crm";
import { errorMessage } from "../lib/errors";
import { Alert, Button, TextField } from "./ui";

/** Two-step form: fill in → review → explicit confirm. Nothing is written before confirming. */
export function ScheduleEventForm({
  defaultTitle = "",
  contactId,
  callId,
  onDone,
}: {
  defaultTitle?: string;
  contactId?: string;
  callId?: string;
  onDone?: () => void;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState(defaultTitle);
  const [start, setStart] = useState("");
  const [minutes, setMinutes] = useState(30);
  const [review, setReview] = useState<NewCalendarEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: (event: NewCalendarEvent) => calendarApi.create(event),
    onSuccess: () => {
      setReview(null);
      setTitle(defaultTitle);
      setStart("");
      queryClient.invalidateQueries({ queryKey: ["calendar"] });
      queryClient.invalidateQueries({ queryKey: ["timeline"] });
      onDone?.();
    },
  });

  function onReview(e: FormEvent) {
    e.preventDefault();
    const startDate = new Date(start);
    if (!title.trim() || Number.isNaN(startDate.getTime())) {
      setError("Enter a title and a start time.");
      return;
    }
    setError(null);
    setReview({
      title: title.trim(),
      starts_at: startDate.toISOString(),
      ends_at: new Date(startDate.getTime() + minutes * 60_000).toISOString(),
      contact_id: contactId,
      call_id: callId,
    });
  }

  if (review) {
    return (
      <div className="space-y-3 rounded-md border border-slate-300 bg-slate-50 p-3" aria-label="Review calendar event">
        <p className="text-sm text-slate-700">This event will be added to your Google Calendar:</p>
        <p className="text-sm font-medium text-slate-900">{review.title}</p>
        <p className="text-sm text-slate-600">
          {formatDateTime(review.starts_at)} – {formatDateTime(review.ends_at)}
        </p>
        {create.error ? <Alert>{errorMessage(create.error)}</Alert> : null}
        <div className="flex gap-2">
          <Button disabled={create.isPending} onClick={() => create.mutate(review)}>
            {create.isPending ? "Adding…" : "Confirm and add to calendar"}
          </Button>
          <Button variant="secondary" onClick={() => setReview(null)}>
            Back
          </Button>
        </div>
      </div>
    );
  }
  return (
    <form onSubmit={onReview} className="grid gap-3 sm:grid-cols-3">
      <div className="sm:col-span-3">
        <TextField label="Event title" value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} />
      </div>
      <TextField label="Starts" type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
      <div className="space-y-1">
        <label htmlFor="duration" className="block text-sm font-medium text-slate-700">
          Duration
        </label>
        <select
          id="duration"
          className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
          value={minutes}
          onChange={(e) => setMinutes(Number(e.target.value))}
        >
          {[15, 30, 45, 60, 90].map((m) => (
            <option key={m} value={m}>
              {m} minutes
            </option>
          ))}
        </select>
      </div>
      <div className="flex items-end">
        <Button type="submit" variant="secondary">
          Review
        </Button>
      </div>
      {error ? (
        <div className="sm:col-span-3">
          <Alert>{error}</Alert>
        </div>
      ) : null}
    </form>
  );
}
