import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fakeAuth, renderWithAuth } from "../test/renderWithAuth";
import { RequireAuth } from "./RequireAuth";

const protectedPage = (
  <RequireAuth>
    <div>SECRET CONTENT</div>
  </RequireAuth>
);

describe("RequireAuth", () => {
  it("redirects anonymous users to the login page", () => {
    renderWithAuth(protectedPage, { auth: fakeAuth({ status: "anonymous" }), path: "/dashboard" });
    expect(screen.getByText("LOGIN PAGE")).toBeInTheDocument();
    expect(screen.queryByText("SECRET CONTENT")).not.toBeInTheDocument();
  });

  it("shows a loading state while the session is being restored", () => {
    renderWithAuth(protectedPage, { auth: fakeAuth({ status: "loading" }), path: "/dashboard" });
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("SECRET CONTENT")).not.toBeInTheDocument();
  });

  it("renders the page for authenticated users", () => {
    renderWithAuth(protectedPage, {
      auth: fakeAuth({
        status: "authenticated",
        session: {
          user: { id: "u", email: "a@example.com", full_name: "A", phone: null },
          company: { id: "c", name: "Co" },
          role: "OWNER",
        },
      }),
      path: "/dashboard",
    });
    expect(screen.getByText("SECRET CONTENT")).toBeInTheDocument();
  });
});
