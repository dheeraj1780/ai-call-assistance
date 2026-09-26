import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { TEST_SESSION, fakeAuth, renderWithAuth } from "../test/renderWithAuth";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/api", async (orig) => ({ ...(await orig<typeof import("../lib/api")>()), apiFetch: api.apiFetch }));
const { KnowledgePage } = await import("./KnowledgePage");

const auth = fakeAuth({ status: "authenticated", session: { ...TEST_SESSION, role: "OWNER" } });
const doc = (over: Record<string, unknown>) => ({
  id: "d1", title: "Price list", filename: "prices.md", size_bytes: 2048, version: 1, status: "READY",
  error_code: null, chunk_count: 3, mime_type: "text/markdown", embedding_model: "gemini:gemini-embedding-001",
  needs_reprocess: false, created_at: "2026-09-26T10:00:00Z", ...over,
});

beforeEach(() => api.apiFetch.mockReset());

describe("KnowledgePage", () => {
  it("lists documents with metadata and offers re-processing only when needed", async () => {
    api.apiFetch.mockImplementation(async (path: string, opts?: { method?: string }) => {
      if (path === "/api/v1/knowledge/documents" && !opts?.method)
        return [doc({}), doc({ id: "d2", title: "Old guide", needs_reprocess: true, embedding_model: "hashing:hashing-v1-1024" })];
      return doc({});
    });
    renderWithAuth(<KnowledgePage />, { auth });
    expect(await screen.findByText("Price list")).toBeInTheDocument();
    expect(screen.getByText(/gemini:gemini-embedding-001/)).toBeInTheDocument();
    expect(screen.getByText(/previous embedding model/)).toBeInTheDocument();
    const buttons = screen.getAllByRole("button", { name: "Re-process" });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]!);
    await waitFor(() =>
      expect(api.apiFetch).toHaveBeenCalledWith("/api/v1/knowledge/documents/d2/reprocess", { method: "POST" }),
    );
  });
});
