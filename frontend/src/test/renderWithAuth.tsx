import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/context";

export function fakeAuth(overrides: Partial<AuthContextValue> = {}): AuthContextValue {
  return {
    status: "anonymous",
    session: null,
    login: vi.fn().mockResolvedValue(undefined),
    register: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

/** Renders `element` at `path`, with marker routes for "/" and "/login" to observe redirects. */
export function renderWithAuth(
  element: ReactElement,
  { auth = fakeAuth(), path = "/login" }: { auth?: AuthContextValue; path?: string } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={path} element={element} />
            {path !== "/" ? <Route path="/" element={<div>HOME PAGE</div>} /> : null}
            {path !== "/login" ? <Route path="/login" element={<div>LOGIN PAGE</div>} /> : null}
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
}
