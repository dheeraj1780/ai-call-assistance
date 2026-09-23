import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { fakeAuth, renderWithAuth } from "../test/renderWithAuth";
import { RegisterPage } from "./RegisterPage";

async function fillForm(password = "correct-horse-battery") {
  await userEvent.type(screen.getByLabelText("Your name"), "Priya Sharma");
  await userEvent.type(screen.getByLabelText("Company name"), "Sharma Traders");
  await userEvent.type(screen.getByLabelText("Work email"), "priya@example.com");
  await userEvent.type(screen.getByLabelText("Password"), password);
  await userEvent.click(screen.getByRole("button", { name: "Create account" }));
}

describe("RegisterPage", () => {
  it("rejects short passwords client-side", async () => {
    const auth = fakeAuth();
    renderWithAuth(<RegisterPage />, { auth, path: "/register" });
    await fillForm("short");
    expect(screen.getByText("Use at least 10 characters.")).toBeInTheDocument();
    expect(auth.register).not.toHaveBeenCalled();
  });

  it("registers and navigates home", async () => {
    const auth = fakeAuth();
    renderWithAuth(<RegisterPage />, { auth, path: "/register" });
    await fillForm();
    expect(auth.register).toHaveBeenCalledWith({
      full_name: "Priya Sharma",
      company_name: "Sharma Traders",
      email: "priya@example.com",
      password: "correct-horse-battery",
    });
    expect(await screen.findByText("HOME PAGE")).toBeInTheDocument();
  });

  it("shows a conflict error for an existing email", async () => {
    const auth = fakeAuth({
      register: vi.fn().mockRejectedValue(
        new ApiError(409, { code: "email_taken", message: "An account with this email already exists" }),
      ),
    });
    renderWithAuth(<RegisterPage />, { auth, path: "/register" });
    await fillForm();
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});
