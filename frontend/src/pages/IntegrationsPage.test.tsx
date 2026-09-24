import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { buildConfigureBody, type Integration, type IntegrationDetail } from "../lib/integrations";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  configure: vi.fn(),
  test: vi.fn(),
  setCapability: vi.fn(),
  disconnect: vi.fn(),
  teamsConnection: vi.fn(),
  teamsConnect: vi.fn(),
  teamsDisconnect: vi.fn(),
  teamsAdminConsentUrl: vi.fn(),
  teamsChats: vi.fn(),
  teamsLinkChat: vi.fn(),
  simulateMessage: vi.fn(),
}));
vi.mock("../lib/integrations", async (orig) => ({ ...(await orig<typeof import("../lib/integrations")>()), integrationsApi: api }));
const { IntegrationsPage } = await import("./IntegrationsPage");

const admin = fakeAuth({ status: "authenticated", session: TEST_SESSION });
const member = fakeAuth({ status: "authenticated", session: { ...TEST_SESSION, role: "MEMBER" } });

const whatsapp: Integration = {
  provider: "WHATSAPP",
  slug: "whatsapp",
  name: "WhatsApp Business",
  subtitle: "WhatsApp Business Platform (Cloud API)",
  channel: "WHATSAPP",
  mode: "LIVE",
  status: "VALIDATED",
  configured: true,
  validated: true,
  enabled: false,
  last_tested_at: null,
  error_state: null,
  mock_allowed: true,
  capabilities: [
    { capability: "MESSAGE", label: "Messages", description: "d", channel: "WHATSAPP", state: "VALIDATED", enabled: false, available: true, reasons: [] },
    {
      capability: "WHATSAPP_VOICE_CALL", label: "Voice Calls", description: "d", channel: "WHATSAPP", state: "NOT_AVAILABLE",
      enabled: false, available: false, reasons: ["Not available in this version: needs WebRTC media"],
    },
  ],
};

const detail: IntegrationDetail = {
  ...whatsapp,
  fields: [
    { key: "phone_number_id", label: "Phone number ID", kind: "text", required: true, help: "h", pattern: null, max_length: 32, options: [], value: "111", is_set: true, display: null },
    { key: "access_token", label: "Access token", kind: "secret", required: true, help: "h", pattern: null, max_length: 1024, options: [], value: null, is_set: true, display: "********" },
  ],
  requirements: [
    { key: "public_url", label: "Public HTTPS URL for Meta webhooks", ok: true, scope: "server", capability: "MESSAGE", hint: null },
    { key: "webhook_verified", label: "Webhook registered and verified", ok: false, scope: "provider", capability: "MESSAGE", hint: "Paste the callback URL" },
  ],
  last_test: { ok: true, checks: [{ key: "phone_number", label: "Token can read the number", ok: true, detail: null, required: true }], missing_permissions: [], account_label: "Acme" },
  setup: { webhook_url: "https://cc.example.com/api/v1/integrations/whatsapp/webhooks/abc" },
  docs: "docs/integrations/whatsapp.md",
};

beforeEach(() => {
  Object.values(api).forEach((m) => m.mockReset());
  api.list.mockResolvedValue([whatsapp]);
  api.get.mockResolvedValue(detail);
});

describe("IntegrationsPage", () => {
  it("renders capabilities exactly as reported by the backend", async () => {
    renderWithAuth(<IntegrationsPage />, { auth: admin, path: "/settings/integrations" });
    const card = await screen.findByRole("region", { name: "WhatsApp Business" });
    expect(within(card).getByText("Connected (validated)")).toBeInTheDocument();
    expect(within(card).getByText("Messages")).toBeInTheDocument();
    expect(within(card).getByText("Voice Calls")).toBeInTheDocument();
    expect(within(card).getByText(/needs WebRTC media/)).toBeInTheDocument();
  });

  it("never shows stored secrets and only sends changed fields", async () => {
    api.configure.mockResolvedValue(detail);
    renderWithAuth(<IntegrationsPage />, { auth: admin, path: "/settings/integrations" });
    await userEvent.click(await screen.findByRole("button", { name: "Configure" }));
    const token = await screen.findByLabelText("Access token");
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveValue("");
    expect(token).toHaveAttribute("placeholder", expect.stringContaining("********"));
    expect(screen.getByText("Configuration incomplete")).toBeInTheDocument();
    expect(screen.getByText(/Paste the callback URL/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.configure).toHaveBeenCalledWith("whatsapp", { mode: "LIVE", values: {} }));
  });

  it("tests the connection and enables a validated capability", async () => {
    api.test.mockResolvedValue(detail);
    api.setCapability.mockResolvedValue(detail);
    renderWithAuth(<IntegrationsPage />, { auth: admin, path: "/settings/integrations" });
    await userEvent.click(await screen.findByRole("button", { name: "Configure" }));
    await userEvent.click(await screen.findByRole("button", { name: "Test Connection" }));
    expect(api.test).toHaveBeenCalledWith("whatsapp");
    await userEvent.click(screen.getByRole("button", { name: "Enable Messages" }));
    expect(api.setCapability).toHaveBeenCalledWith("whatsapp", "MESSAGE", true);
  });

  it("members can view but not configure", async () => {
    renderWithAuth(<IntegrationsPage />, { auth: member, path: "/settings/integrations" });
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    await screen.findByText("Configuration incomplete");
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Test Connection" })).not.toBeInTheDocument();
  });
});

describe("buildConfigureBody", () => {
  it("keeps secrets when left blank and sends new ones", () => {
    expect(buildConfigureBody(detail.fields, { phone_number_id: "111", access_token: "" }, "LIVE")).toEqual({ mode: "LIVE", values: {} });
    expect(buildConfigureBody(detail.fields, { phone_number_id: "222", access_token: " new " }, "LIVE")).toEqual({
      mode: "LIVE",
      values: { phone_number_id: "222", access_token: "new" },
    });
  });
});
