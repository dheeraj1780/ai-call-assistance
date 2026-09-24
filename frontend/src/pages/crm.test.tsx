import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { validateContact } from "../lib/contactForm";
import type { ActionItem, Call, Contact, Page } from "../lib/crm";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const mocks = vi.hoisted(() => ({
  listContacts: vi.fn(),
  getContact: vi.fn(),
  createContact: vi.fn(),
  timeline: vi.fn(),
  listMembers: vi.fn(),
  listActionItems: vi.fn(),
  confirmActionItem: vi.fn(),
  updateActionItem: vi.fn(),
  getCall: vi.fn(),
  updateCall: vi.fn(),
}));

vi.mock("../lib/crm", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/crm")>();
  return { ...actual, crm: { ...actual.crm, ...mocks } };
});

const { ContactsPage } = await import("./ContactsPage");
const { ContactDetailPage } = await import("./ContactDetailPage");
const { ContactFormPage } = await import("./ContactFormPage");
const { CallDetailPage } = await import("./CallDetailPage");
const { ActionItemList } = await import("../components/ActionItemList");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 25, offset: 0 };
}

const contact: Contact = {
  id: "k1",
  name: "Ravi Kumar",
  organization: "ABC Industries",
  phone: "+919876543210",
  email: "ravi@abc.in",
  designation: "Owner",
  status: "QUALIFIED",
  source: "REFERRAL",
  tags: ["vip"],
  attributes: {},
  notes: null,
  owner_user_id: "u1",
  created_at: "2026-09-20T10:00:00Z",
  updated_at: "2026-09-21T10:00:00Z",
};

beforeEach(() => {
  Object.values(mocks).forEach((m) => m.mockReset());
  mocks.listMembers.mockResolvedValue(
    page([{ user_id: "u1", email: "priya@example.com", full_name: "Priya Sharma", role: "OWNER" }]),
  );
});

describe("ContactsPage", () => {
  it("lists contacts and searches", async () => {
    mocks.listContacts.mockResolvedValue(page([contact]));
    renderWithAuth(<ContactsPage />, { auth, path: "/contacts" });

    expect(await screen.findByText("Ravi Kumar")).toBeInTheDocument();
    expect(screen.getByText(/ABC Industries/)).toBeInTheDocument();
    expect(within(screen.getByRole("list")).getByText("Qualified")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search contacts"), "abc");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(mocks.listContacts).toHaveBeenLastCalledWith(expect.objectContaining({ q: "abc", offset: 0 })),
    );
  });

  it("shows an empty state", async () => {
    mocks.listContacts.mockResolvedValue(page([]));
    renderWithAuth(<ContactsPage />, { auth, path: "/contacts" });
    expect(await screen.findByText(/No contacts yet/)).toBeInTheDocument();
  });
});

describe("contact form", () => {
  it("validates required name, email and phone", () => {
    const base = {
      name: "",
      organization: "",
      phone: "12",
      email: "bad",
      designation: "",
      status: "NEW",
      source: "MANUAL",
      tags: "",
      notes: "",
      owner_user_id: "",
    };
    expect(validateContact(base)).toEqual({
      name: "Name is required.",
      email: "Enter a valid email.",
      phone: "Use 6–15 digits, optionally starting with +.",
    });
    expect(validateContact({ ...base, name: "A", phone: "+91 98765-43210", email: "" })).toEqual({});
  });

  it("creates a contact and does not send blank optional fields", async () => {
    mocks.createContact.mockResolvedValue(contact);
    renderWithAuth(<ContactFormPage />, { auth, path: "/contacts/new" });
    await userEvent.type(screen.getByLabelText("Name"), "Ravi Kumar");
    await userEvent.type(screen.getByLabelText("Tags"), "vip, north");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.createContact).toHaveBeenCalled());
    const body = mocks.createContact.mock.calls[0]![0];
    expect(body).toMatchObject({ name: "Ravi Kumar", email: null, phone: null, tags: ["vip", "north"] });
    expect(body).not.toHaveProperty("owner_user_id"); // defaults to the creator server-side
  });
});

describe("ContactDetailPage", () => {
  it("renders contact info and the timeline", async () => {
    mocks.getContact.mockResolvedValue(contact);
    mocks.timeline.mockResolvedValue({
      items: [
        {
          id: "e1",
          contact_id: "k1",
          category: "STATUS_CHANGE",
          event_type: "STATUS_CHANGED",
          occurred_at: "2026-09-21T10:00:00Z",
          actor_user_id: "u1",
          summary: "Status changed from NEW to QUALIFIED",
          from_status: "NEW",
          to_status: "QUALIFIED",
          note_id: null,
          call_id: null,
          action_item_id: null,
        },
      ],
      next_cursor: null,
    });
    renderWithAuth(<ContactDetailPage />, { auth, path: "/contacts/:id", url: "/contacts/k1" });
    expect(await screen.findByRole("heading", { name: "Ravi Kumar" })).toBeInTheDocument();
    expect(await screen.findByText("Status changed from NEW to QUALIFIED")).toBeInTheDocument();
    expect(mocks.timeline).toHaveBeenCalledWith("k1", expect.objectContaining({ limit: 20 }));

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("alert")).toHaveTextContent("cannot be undone");
  });
});

const aiItem: ActionItem = {
  id: "a1",
  title: "Send brochure",
  description: null,
  kind: "FOLLOW_UP",
  status: "OPEN",
  source: "AI",
  contact_id: "k1",
  contact: { id: "k1", name: "Ravi Kumar", organization: null },
  call_id: null,
  assignee_user_id: "u1",
  created_by_user_id: "u1",
  due_at: null,
  confirmed_at: null,
  is_confirmed: false,
  completed_at: null,
  created_at: "2026-09-21T10:00:00Z",
  updated_at: "2026-09-21T10:00:00Z",
};

describe("ActionItemList", () => {
  it("requires confirmation of AI suggestions before completion", async () => {
    mocks.confirmActionItem.mockResolvedValue({ ...aiItem, is_confirmed: true });
    renderWithAuth(<ActionItemList items={[aiItem]} />, { auth, path: "/action-items" });
    expect(screen.getByText("AI suggestion")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: 'Mark "Send brochure" done' })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(mocks.confirmActionItem).toHaveBeenCalledWith("a1"));
  });

  it("marks a confirmed item done", async () => {
    const item = { ...aiItem, source: "MANUAL" as const, is_confirmed: true, confirmed_at: "x" };
    mocks.updateActionItem.mockResolvedValue({ ...item, status: "DONE" });
    renderWithAuth(<ActionItemList items={[item]} />, { auth, path: "/action-items" });
    await userEvent.click(screen.getByRole("checkbox", { name: 'Mark "Send brochure" done' }));
    await waitFor(() => expect(mocks.updateActionItem).toHaveBeenCalledWith("a1", { status: "DONE" }));
  });
});

describe("CallDetailPage", () => {
  it("offers manual status actions for a planned call", async () => {
    const call: Call = {
      id: "c9",
      contact_id: "k1",
      contact: { id: "k1", name: "Ravi Kumar", organization: null },
      user_id: "u1",
      objective: "Understand inventory needs",
      desired_outcome: null,
      status: "PLANNED",
      scheduled_at: null,
      started_at: null,
      ended_at: null,
      duration_seconds: null,
      outcome: null,
      outcome_notes: null,
      next_step: null,
      created_at: "2026-09-21T10:00:00Z",
      updated_at: "2026-09-21T10:00:00Z",
    };
    mocks.getCall.mockResolvedValue(call);
    mocks.listActionItems.mockResolvedValue(page([]));
    mocks.updateCall.mockResolvedValue({ ...call, status: "CANCELLED" });
    renderWithAuth(<CallDetailPage />, { auth, path: "/calls/:id", url: "/calls/c9" });
    expect(await screen.findByText("Understand inventory needs")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(mocks.updateCall).toHaveBeenCalledWith("c9", { status: "CANCELLED" }));
  });
});
