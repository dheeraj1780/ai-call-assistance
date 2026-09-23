import { createContext, useContext } from "react";

import type { SessionInfo } from "../lib/types";

export type AuthStatus = "loading" | "authenticated" | "anonymous";

export interface RegisterInput {
  email: string;
  password: string;
  full_name: string;
  company_name: string;
}

export interface AuthContextValue {
  status: AuthStatus;
  session: SessionInfo | null;
  login: (email: string, password: string) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
