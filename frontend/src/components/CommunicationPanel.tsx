import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError } from "../lib/api";
import { conversationsApi } from "../lib/conversations";
import { errorMessage } from "../lib/errors";
import { integrationsApi } from "../lib/integrations";
import { Card } from "./common";
import { Alert, Button } from "./ui";

/** Communication actions for a contact. Only actions the backend reports as available
 * (configured, enabled, authorised) are shown - no provider logic lives here. */
export function CommunicationPanel({ contactId }: { contactId: string }) {
  const navigate = useNavigate();
  const options = useQuery({ queryKey: ["communication", contactId], queryFn: () => conversationsApi.options(contactId) });
  const [pickChat, setPickChat] = useState(false);
  const open = useMutation({
    mutationFn: (channel: "WHATSAPP" | "TEAMS") => conversationsApi.open(contactId, channel),
    onSuccess: (session) => navigate(`/conversations/${session.id}`),
    onError: (error) => {
      if (error instanceof ApiError && error.code === "teams_chat_not_linked") setPickChat(true);
    },
  });
  const available = options.data?.filter((o) => o.available) ?? [];

  function act(key: string) {
    if (key === "whatsapp_message") open.mutate("WHATSAPP");
    else if (key === "teams_message") open.mutate("TEAMS");
    else if (key === "teams_call") navigate(`/contacts/${contactId}/prepare?channel=TEAMS`);
    else if (key === "google_meet_call") navigate(`/contacts/${contactId}/prepare?channel=GOOGLE_MEET`);
    else if (key === "phone_call") navigate(`/contacts/${contactId}/prepare`);
  }

  if (options.isPending || options.error) return null;
  return (
    <Card title="Communication">
      {available.length === 0 ? (
        <p className="text-sm text-slate-500">
          No channels are enabled yet. <Link className="underline" to="/settings/integrations">Set up integrations</Link>
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {available.map((o) => (
            <Button key={o.key} variant="secondary" onClick={() => act(o.key)} disabled={open.isPending}>
              {o.label}
              {o.mock ? <span className="ml-1 text-xs text-slate-400">(mock)</span> : null}
            </Button>
          ))}
        </div>
      )}
      {open.error && !pickChat ? <Alert>{errorMessage(open.error)}</Alert> : null}
      {pickChat ? <TeamsChatPicker contactId={contactId} onLinked={(sid) => navigate(`/conversations/${sid}`)} /> : null}
    </Card>
  );
}

function TeamsChatPicker({ contactId, onLinked }: { contactId: string; onLinked: (sessionId: string) => void }) {
  const chats = useQuery({ queryKey: ["teams-chats"], queryFn: integrationsApi.teamsChats });
  const link = useMutation({
    mutationFn: (chatId: string) => integrationsApi.teamsLinkChat(chatId, contactId),
    onSuccess: (r) => onLinked(r.session_id),
  });
  return (
    <div className="mt-3 space-y-2 rounded-md border border-slate-200 p-3">
      <p className="text-sm text-slate-700">Choose the Teams chat you have with this customer:</p>
      {chats.error ? <Alert>{errorMessage(chats.error)}</Alert> : null}
      <ul className="space-y-1">
        {chats.data?.map((c) => (
          <li key={c.id} className="flex items-center justify-between gap-2 text-sm">
            <span className="truncate">{c.topic || (c.chat_type === "oneOnOne" ? "1:1 chat" : "Group chat")}</span>
            <Button variant="secondary" onClick={() => link.mutate(c.id)} disabled={link.isPending}>
              Link
            </Button>
          </li>
        ))}
      </ul>
      {link.error ? <Alert>{errorMessage(link.error)}</Alert> : null}
    </div>
  );
}
