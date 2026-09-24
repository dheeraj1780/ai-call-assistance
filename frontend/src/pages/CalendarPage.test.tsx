import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const mocks = vi.hoisted(() => ({
  connection: vi.fn(),
  connect: vi.fn(),
  disconnect: vi.fn(),
  upcoming: vi.fn(),
  appEvents: vi.fn(),
  create: vi.fn(),
}));
vi.mock("../lib/calendar", () => ({ calendarApi: mocks }));
const { CalendarPage } = await import("./CalendarPage");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });
beforeEach(() => Object.values(mocks).forEach((m) => m.mockReset()));

describe("CalendarPage", () => {
  it("offers to connect when not connected", async () => {
    mocks.connection.mockResolvedValue({ connected: false, provider: null, account_email: null, status: null });
    renderWithAuth(<CalendarPage />, { auth, path: "/calendar" });
    expect(await screen.findByRole("button", { name: "Connect Google Calendar" })).toBeInTheDocument();
    expect(mocks.upcoming).not.toHaveBeenCalled();
  });

  it("creates an event only after explicit confirmation", async () => {
    mocks.connection.mockResolvedValue({ connected: true, provider: "google", account_email: "a@b.c", status: "ACTIVE" });
    mocks.upcoming.mockResolvedValue([]);
    mocks.create.mockResolvedValue({});
    renderWithAuth(<CalendarPage />, { auth, path: "/calendar" });

    await screen.findByText("No upcoming events.");
    await userEvent.type(screen.getByLabelText("Starts"), "2030-01-10T10:00");
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(mocks.create).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Review calendar event")).toHaveTextContent("Follow-up call");

    await userEvent.click(screen.getByRole("button", { name: "Confirm and add to calendar" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    expect(mocks.create.mock.calls[0]![0]).toMatchObject({ title: "Follow-up call" });
  });

  it("asks to reconnect when the grant is revoked", async () => {
    mocks.connection.mockResolvedValue({ connected: true, provider: "google", account_email: null, status: "ACTIVE" });
    mocks.upcoming.mockRejectedValue(new ApiError(409, { code: "calendar_reauth_required", message: "x" }));
    renderWithAuth(<CalendarPage />, { auth, path: "/calendar" });
    expect(await screen.findByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  });
});
