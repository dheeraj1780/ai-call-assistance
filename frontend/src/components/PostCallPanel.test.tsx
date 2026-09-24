import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PostCall } from "../lib/postcall";
import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const pc = vi.hoisted(() => ({ get: vi.fn(), retry: vi.fn(), reviewDraft: vi.fn() }));
vi.mock("../lib/postcall", async (orig) => ({ ...(await orig<typeof import("../lib/postcall")>()), postCallApi: pc }));
const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api", async (orig) => ({ ...(await orig<typeof import("../lib/api")>()), apiFetch: api.apiFetch }));
const { PostCallPanel } = await import("./PostCallPanel");

const auth = fakeAuth({ status: "authenticated", session: TEST_SESSION });
const field = (value: string | null, status: "CONFIRMED" | "INFERRED" | "NOT_DISCUSSED") => ({ value, status });

function bundle(overrides: Partial<PostCall> = {}): PostCall {
  return {
    call: {} as PostCall["call"],
    auto_send: false,
    summary: {
      status: "READY", provider: "mock", error_code: null, summary: "Call with Ravi.", suggested_outcome: "FOLLOW_UP_REQUIRED",
      current_solution: field("Uses Excel", "CONFIRMED"), budget: field("2 lakh", "INFERRED"), timeline: field(null, "NOT_DISCUSSED"),
      decision_maker: field(null, "NOT_DISCUSSED"), next_step: field("Send quote", "CONFIRMED"), generated_at: "x",
    },
    drafts: [
      { id: "d1", channel: "EMAIL", subject: "Following up", body: "Hello Ravi", status: "DRAFT", source: "AI", warnings: ["Contains a figure not mentioned in the call: 25%"], reviewed_at: null },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  Object.values(pc).forEach((m) => m.mockReset());
  api.apiFetch.mockReset().mockResolvedValue([]);
});

describe("PostCallPanel", () => {
  it("labels grounded, inferred and missing fields and shows draft warnings", async () => {
    pc.get.mockResolvedValue(bundle());
    renderWithAuth(<PostCallPanel callId="c1" />, { auth, path: "/calls/c1" });
    expect(await screen.findByText("Call with Ravi.")).toBeInTheDocument();
    expect(screen.getAllByText("CONFIRMED")).toHaveLength(2);
    expect(screen.getByText("INFERRED")).toBeInTheDocument();
    expect(screen.getAllByText("NOT DISCUSSED")).toHaveLength(2);
    const draft = screen.getByRole("article", { name: "Email draft" });
    expect(within(draft).getByRole("note")).toHaveTextContent("25%");
    expect(screen.getByText(/never sent automatically/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
  });

  it("requires saving edits before approving and offers retry on failure", async () => {
    pc.get.mockResolvedValueOnce(bundle());
    pc.reviewDraft.mockResolvedValue({});
    renderWithAuth(<PostCallPanel callId="c1" />, { auth, path: "/calls/c1" });
    const box = await screen.findByLabelText("Email message");
    await userEvent.type(box, " there");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save edits" }));
    await waitFor(() => expect(pc.reviewDraft).toHaveBeenCalledWith("c1", "d1", { body: "Hello Ravi there" }));
  });

  it("shows a retry when post-call processing failed", async () => {
    const b = bundle();
    pc.get.mockResolvedValue({ ...b, summary: { ...b.summary!, status: "FAILED", error_code: "ai_unavailable" }, drafts: [] });
    pc.retry.mockResolvedValue(undefined);
    renderWithAuth(<PostCallPanel callId="c1" />, { auth, path: "/calls/c1" });
    expect(await screen.findByRole("alert")).toHaveTextContent("call record and transcript are safe");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(pc.retry).toHaveBeenCalledWith("c1"));
  });
});
