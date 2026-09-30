import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const crmMocks = vi.hoisted(() => ({ getContact: vi.fn(), createCall: vi.fn() }));
vi.mock("../lib/crm", async (orig) => {
  const actual = await orig<typeof import("../lib/crm")>();
  return { ...actual, crm: { ...actual.crm, ...crmMocks } };
});
const conv = vi.hoisted(() => ({ options: vi.fn() }));
vi.mock("../lib/conversations", async (orig) => {
  const actual = await orig<typeof import("../lib/conversations")>();
  return { ...actual, conversationsApi: { ...actual.conversationsApi, options: conv.options } };
});
const { PlanCallPage } = await import("./PlanCallPage");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });
const action = (key: string, channel: string, capability: string, available: boolean, reason: string | null = null) => ({
  key, channel, capability, label: key, available, reason, mock: false, provider: null,
});

beforeEach(() => {
  Object.values(crmMocks).forEach((m) => m.mockReset());
  conv.options.mockReset();
  crmMocks.getContact.mockResolvedValue({ id: "k1", name: "Ravi Kumar" });
  crmMocks.createCall.mockResolvedValue({ id: "c9" });
});

describe("PlanCallPage", () => {
  it("plans a phone call without any Microsoft 365 / Google setup", async () => {
    conv.options.mockResolvedValue([
      action("teams_call", "TEAMS", "REAL_TIME_CALL", false, "Teams call copilot is not enabled"),
      action("google_meet_call", "GOOGLE_MEET", "REAL_TIME_CALL", false),
      action("phone_call", "PHONE", "PHONE_CALL", true),
      action("whatsapp_message", "WHATSAPP", "MESSAGE", true),
    ]);
    renderWithAuth(<PlanCallPage />, { auth, path: "/contacts/:id/prepare", url: "/contacts/k1/prepare" });
    const select = await screen.findByLabelText("How will you talk?");
    expect(select).toHaveValue("PHONE");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).not.toContain("Microsoft Teams");
    expect(screen.queryByLabelText(/Teams meeting link/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to agenda" }));
    await waitFor(() => expect(crmMocks.createCall).toHaveBeenCalledWith(expect.objectContaining({ contact_id: "k1", channel: "PHONE" })));
    expect(crmMocks.createCall.mock.calls[0]?.[0]).not.toHaveProperty("meeting_url");
  });

  it("offers Teams only when it is enabled, and then asks for the meeting link", async () => {
    conv.options.mockResolvedValue([
      action("teams_call", "TEAMS", "REAL_TIME_CALL", true),
      action("phone_call", "PHONE", "PHONE_CALL", true),
    ]);
    renderWithAuth(<PlanCallPage />, { auth, path: "/contacts/:id/prepare", url: "/contacts/k1/prepare?channel=TEAMS" });
    expect(await screen.findByLabelText("How will you talk?")).toHaveValue("TEAMS");
    expect(screen.getByLabelText(/Teams meeting link/)).toBeInTheDocument();
  });

  it("explains how to enable phone calling when no channel is available", async () => {
    conv.options.mockResolvedValue([action("phone_call", "PHONE", "PHONE_CALL", false, "Phone calling is not enabled")]);
    renderWithAuth(<PlanCallPage />, { auth, path: "/contacts/:id/prepare", url: "/contacts/k1/prepare" });
    expect(await screen.findByText(/A Microsoft 365 or Google account is not required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to agenda" })).toBeDisabled();
  });
});
