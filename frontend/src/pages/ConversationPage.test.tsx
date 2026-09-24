import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelAction, Conversation } from "../lib/conversations";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  send: vi.fn(),
  updateDraft: vi.fn(),
  link: vi.fn(),
  open: vi.fn(),
  options: vi.fn(),
}));
vi.mock("../lib/conversations", async (orig) => ({
  ...(await orig<typeof import("../lib/conversations")>()),
  conversationsApi: api,
}));
const { ConversationPage } = await import("./ConversationPage");
const { CommunicationPanel } = await import("../components/CommunicationPanel");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });

function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    session: {
      id: "s1", provider: "WHATSAPP", channel: "WHATSAPP", capability: "MESSAGE", status: "OPEN", match_status: "MATCHED",
      contact: { id: "k1", name: "ABC Industries", organization: null }, call_id: null, display_name: "Ravi",
      external_participant_id: "91", last_message_at: null, last_inbound_at: null, mock: false, preview: null,
    },
    messages: [
      { id: "m1", direction: "INBOUND", sender_type: "CUSTOMER", sender_user_id: null, message_type: "TEXT", content: "Can you send the quotation?", status: "RECEIVED", error_code: null, occurred_at: "2026-09-24T10:00:00Z" },
    ],
    drafts: [
      { id: "d1", reply_to_message_id: "m1", body: "Sure. I'll prepare the quotation for 100 units and send it shortly.", source: "AI", status: "SUGGESTED", warnings: ["Contains a figure not mentioned in the call: 100"], ai_provider: "anthropic", created_at: "x" },
    ],
    notes: [],
    can_send: true,
    send_blocked_reason: null,
    ...overrides,
  };
}

beforeEach(() => {
  Object.values(api).forEach((m) => m.mockReset());
});

describe("ConversationPage", () => {
  it("shows the AI suggestion but never sends without an explicit Send", async () => {
    api.get.mockResolvedValue(conversation());
    api.send.mockResolvedValue({});
    renderWithAuth(<ConversationPage />, { auth, path: "/conversations/:id", url: "/conversations/s1" });
    expect(await screen.findByText("Can you send the quotation?")).toBeInTheDocument();
    expect(screen.getByText(/prepare the quotation for 100 units/)).toBeInTheDocument();
    expect(screen.getByText(/figure not mentioned/)).toBeInTheDocument();
    expect(screen.getByText("WhatsApp")).toBeInTheDocument();
    expect(api.send).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Message");
    expect(box).toHaveValue("Sure. I'll prepare the quotation for 100 units and send it shortly.");
    await userEvent.clear(box);
    await userEvent.type(box, "Sure, sharing the quotation today.");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
    const body = api.send.mock.calls[0]![1];
    expect(body).toMatchObject({ text: "Sure, sharing the quotation today.", draft_id: "d1" });
    expect(body.client_message_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("explains why sending is blocked", async () => {
    api.get.mockResolvedValue(conversation({ can_send: false, send_blocked_reason: "WhatsApp only allows free-form messages within 24 hours", drafts: [] }));
    renderWithAuth(<ConversationPage />, { auth, path: "/conversations/:id", url: "/conversations/s1" });
    expect(await screen.findByText(/within 24 hours/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("asks a human to link an unmatched conversation", async () => {
    const c = conversation();
    api.get.mockResolvedValue({ ...c, session: { ...c.session, match_status: "UNMATCHED", contact: null } });
    renderWithAuth(<ConversationPage />, { auth, path: "/conversations/:id", url: "/conversations/s1" });
    expect(await screen.findByText(/not linked to a contact yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Link to contact" })).toBeDisabled();
  });
});

describe("CommunicationPanel", () => {
  const option = (key: string, label: string, available: boolean): ChannelAction => ({
    key, label, available, channel: "X", capability: "Y", reason: available ? null : "not enabled", mock: false, provider: null,
  });

  it("shows only the actions the backend reports as available", async () => {
    api.options.mockResolvedValue([
      option("whatsapp_message", "WhatsApp Message", true),
      option("teams_message", "Teams Message", false),
      option("teams_call", "Teams Call", true),
      option("phone_call", "Phone Call", false),
    ]);
    renderWithAuth(<CommunicationPanel contactId="k1" />, { auth, path: "/contacts/k1" });
    expect(await screen.findByRole("button", { name: "WhatsApp Message" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Teams Call" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Teams Message" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Phone Call" })).not.toBeInTheDocument();
  });

  it("links to setup when no channel is available", async () => {
    api.options.mockResolvedValue([option("phone_call", "Phone Call", false)]);
    renderWithAuth(<CommunicationPanel contactId="k1" />, { auth, path: "/contacts/k1" });
    expect(await screen.findByRole("link", { name: "Set up integrations" })).toHaveAttribute("href", "/settings/integrations");
  });
});
