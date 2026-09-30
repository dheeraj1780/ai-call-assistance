import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CallPrep } from "../lib/prep";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const mocks = vi.hoisted(() => ({ prep: vi.fn(), saveAgenda: vi.fn(), suggest: vi.fn(), setItemStatus: vi.fn() }));
vi.mock("../lib/prep", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/prep")>();
  return { ...actual, prepApi: mocks };
});
const crmMocks = vi.hoisted(() => ({ updateCall: vi.fn() }));
vi.mock("../lib/crm", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/crm")>();
  return { ...actual, crm: { ...actual.crm, updateCall: crmMocks.updateCall } };
});
const { CallPrepPage } = await import("./CallPrepPage");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });

const prep: CallPrep = {
  call: {
    id: "c1",
    contact_id: "k1",
    contact: { id: "k1", name: "Ravi Kumar", organization: "ABC" },
    user_id: "u1",
    objective: "Qualify inventory need",
    desired_outcome: "Demo booked",
    status: "PLANNED",
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_seconds: null,
    outcome: null,
    outcome_notes: null,
    next_step: null,
    created_at: "2026-09-24T10:00:00Z",
    updated_at: "2026-09-24T10:00:00Z",
  },
  contact: {
    id: "k1",
    name: "Ravi Kumar",
    organization: "ABC",
    phone: null,
    email: null,
    designation: null,
    status: "NEW",
    source: "MANUAL",
    tags: [],
    attributes: {},
    notes: null,
    owner_user_id: "u1",
    created_at: "2026-09-24T10:00:00Z",
    updated_at: "2026-09-24T10:00:00Z",
  },
  agenda: [],
  previous_calls: [],
  recent_notes: [
    { id: "n1", contact_id: "k1", author_user_id: "u1", body: "Uses Excel", created_at: "x", updated_at: "x" },
  ],
  open_action_items: [],
  known_requirements: [],
  known_objections: [],
  recent_timeline: [],
};

beforeEach(() => Object.values(mocks).forEach((m) => m.mockReset()));

describe("CallPrepPage", () => {
  it("shows context, separates facts from AI suggestions, and saves the chosen agenda", async () => {
    mocks.prep.mockResolvedValue(prep);
    mocks.suggest.mockResolvedValue({
      customer_facts: [{ text: "Uses Excel", source_ref: "note:n1" }],
      ai_suggested_agenda: [
        { title: "Current process", question: "How do you track stock?", why: null },
        { title: "Budget", question: null, why: null },
      ],
      ai_open_questions: [],
      provider: "mock",
      discarded_ungrounded_facts: 0,
    });
    mocks.saveAgenda.mockResolvedValue([]);
    renderWithAuth(<CallPrepPage />, { auth, path: "/calls/:id/prepare", url: "/calls/c1/prepare" });

    expect(await screen.findByText("Qualify inventory need")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start call" })).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: "Suggest with AI" }));
    const panel = await screen.findByLabelText("AI suggestion");
    expect(panel).toHaveTextContent("Customer facts (from your records)");
    expect(panel).toHaveTextContent("AI suggested agenda (offline mock provider)");

    await userEvent.click(screen.getByRole("button", { name: "Use this agenda" }));
    expect(screen.getByLabelText("Agenda item 1 title")).toHaveValue("Current process");
    expect(screen.getByRole("button", { name: "Start call" })).toBeDisabled(); // unsaved

    await userEvent.clear(screen.getByLabelText("Agenda item 2 title"));
    await userEvent.type(screen.getByLabelText("Agenda item 2 title"), "Budget range");
    await userEvent.click(screen.getByRole("button", { name: "Save agenda" }));
    await waitFor(() => expect(mocks.saveAgenda).toHaveBeenCalled());
    const [, items] = mocks.saveAgenda.mock.calls[0]!;
    expect(items.map((i: { title: string; source: string }) => [i.title, i.source])).toEqual([
      ["Current process", "AI"],
      ["Budget range", "AI_EDITED"],
    ]);
  });

  it("edits a planned Teams call's meeting link and language before start", async () => {
    const teams = { ...prep, call: { ...prep.call, channel: "TEAMS" as const, meeting_url: "https://teams.microsoft.com/l/meetup-join/old", language: "en-IN" } };
    mocks.prep.mockResolvedValue(teams);
    crmMocks.updateCall.mockResolvedValue(teams.call);
    renderWithAuth(<CallPrepPage />, { auth, path: "/calls/:id/prepare", url: "/calls/c1/prepare" });
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByRole("button", { name: "Start call" })).toBeDisabled();
    const link = screen.getByLabelText(/Teams meeting link/);
    await userEvent.clear(link);
    await userEvent.type(link, "https://teams.microsoft.com/l/meetup-join/new");
    await userEvent.selectOptions(screen.getByLabelText(/Conversation language/), "hi-IN");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(crmMocks.updateCall).toHaveBeenCalledWith(
        "c1",
        expect.objectContaining({ meeting_url: "https://teams.microsoft.com/l/meetup-join/new", language: "hi-IN" }),
      ),
    );
  });

  it("does not offer Edit once the call has started", async () => {
    mocks.prep.mockResolvedValue({ ...prep, call: { ...prep.call, status: "ACTIVE" } });
    renderWithAuth(<CallPrepPage />, { auth, path: "/calls/:id/prepare", url: "/calls/c1/prepare" });
    expect(await screen.findByText("Details are fixed once the call has started.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });
});
