import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate, useParams } from "react-router";

import { Card, PageHeader, QueryState, TextArea } from "../components/common";
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
  const create = useMutation({
    mutationFn: () =>
      crm.createCall({
        contact_id: id,
        objective: objective.trim() || undefined,
        desired_outcome: desired.trim() || undefined,
        scheduled_at: localInputToIso(when),
      }),
    onSuccess: (call) => {
      queryClient.invalidateQueries({ queryKey: ["calls"] });
      queryClient.invalidateQueries({ queryKey: ["timeline", id] });
      navigate(`/calls/${call.id}`);
    },
  });
  function onSubmit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }
  return (
    <QueryState isPending={contact.isPending} error={contact.error}>
      <div className="mx-auto max-w-2xl">
        <PageHeader title={`Plan a call with ${contact.data?.name ?? ""}`} />
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
            <TextField label="Scheduled for (optional)" type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? "Saving…" : "Plan call"}
            </Button>
          </form>
        </Card>
      </div>
    </QueryState>
  );
}
