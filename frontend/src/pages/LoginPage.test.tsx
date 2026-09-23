import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/api";
import { fakeAuth, renderWithAuth } from "../test/renderWithAuth";
import { LoginPage } from "./LoginPage";

describe("LoginPage", () => {
  it("validates input before calling the API", async () => {
    const auth = fakeAuth();
    renderWithAuth(<LoginPage />, { auth });

    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(auth.login).not.toHaveBeenCalled();
  });

  it("signs in and navigates home", async () => {
    const auth = fakeAuth();
    renderWithAuth(<LoginPage />, { auth });

    await userEvent.type(screen.getByLabelText("Email"), "  priya@example.com ");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(auth.login).toHaveBeenCalledWith("priya@example.com", "correct-horse-battery");
    expect(await screen.findByText("HOME PAGE")).toBeInTheDocument();
  });

  it("shows the server error and stays on the page", async () => {
    const auth = fakeAuth({
      login: vi.fn().mockRejectedValue(
        new ApiError(401, { code: "invalid_credentials", message: "Invalid email or password" }),
      ),
    });
    renderWithAuth(<LoginPage />, { auth });

    await userEvent.type(screen.getByLabelText("Email"), "priya@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
    expect(screen.queryByText("HOME PAGE")).not.toBeInTheDocument();
  });

  it("explains rate limiting in plain language", async () => {
    const auth = fakeAuth({
      login: vi.fn().mockRejectedValue(new ApiError(429, { code: "rate_limited", message: "x" })),
    });
    renderWithAuth(<LoginPage />, { auth });

    await userEvent.type(screen.getByLabelText("Email"), "priya@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "whatever-pass");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many attempts");
  });
});
