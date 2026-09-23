import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { authApi, onSessionExpired, refreshSession, setAccessToken } from "../lib/api";
import type { AuthResponse, SessionInfo } from "../lib/types";
import { AuthContext, type AuthContextValue, type AuthStatus, type RegisterInput } from "./context";

function toSession(response: AuthResponse): SessionInfo {
  return { user: response.user, company: response.company, role: response.role };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [session, setSession] = useState<SessionInfo | null>(null);

  const signedOut = useCallback(() => {
    setAccessToken(null);
    setSession(null);
    setStatus("anonymous");
    queryClient.clear();
  }, [queryClient]);

  const signedIn = useCallback((response: AuthResponse) => {
    setAccessToken(response.access_token);
    setSession(toSession(response));
    setStatus("authenticated");
  }, []);

  // Restore the session from the refresh cookie on first load.
  useEffect(() => {
    let cancelled = false;
    refreshSession()
      .then((response) => {
        if (cancelled) return;
        if (response) signedIn(response);
        else signedOut();
      })
      .catch(() => {
        if (!cancelled) signedOut();
      });
    return () => {
      cancelled = true;
    };
  }, [signedIn, signedOut]);

  useEffect(() => onSessionExpired(signedOut), [signedOut]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      session,
      login: async (email, password) => signedIn(await authApi.login(email, password)),
      register: async (input: RegisterInput) => signedIn(await authApi.register(input)),
      logout: async () => {
        try {
          await authApi.logout();
        } finally {
          signedOut();
        }
      },
    }),
    [status, session, signedIn, signedOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
