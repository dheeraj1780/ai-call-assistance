/**
 * API client.
 *
 * - The access token lives only in memory (never localStorage).
 * - The refresh token is an httpOnly cookie the browser sends to /api/v1/auth only.
 * - On a 401 the client refreshes once and retries. Concurrent refreshes are coalesced
 *   within the tab and serialised across tabs (Web Locks), because refresh tokens rotate.
 */
import type { AuthResponse, ErrorBody } from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const CSRF_HEADER = { "X-CSRF-Protection": "1" };

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | null;

  constructor(status: number, body: Partial<ErrorBody>) {
    super(body.message ?? "Request failed");
    this.name = "ApiError";
    this.status = status;
    this.code = body.code ?? "unknown_error";
    this.details = body.details ?? null;
    this.requestId = body.request_id ?? null;
  }
}

let accessToken: string | null = null;
let refreshInFlight: Promise<AuthResponse | null> | null = null;
const sessionExpiredListeners = new Set<() => void>();

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function onSessionExpired(listener: () => void): () => void {
  sessionExpiredListeners.add(listener);
  return () => sessionExpiredListeners.delete(listener);
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const json = (await response.json()) as { error?: Partial<ErrorBody> };
    return new ApiError(response.status, json.error ?? {});
  } catch {
    return new ApiError(response.status, { message: response.statusText || "Request failed" });
  }
}

async function doRefresh(): Promise<AuthResponse | null> {
  const response = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
    method: "POST",
    credentials: "include",
    headers: CSRF_HEADER,
  });
  if (!response.ok) {
    setAccessToken(null);
    if (response.status === 401) return null;
    throw await parseError(response);
  }
  const body = (await response.json()) as AuthResponse;
  setAccessToken(body.access_token);
  return body;
}

/** Refresh the session; returns null if there is no valid session. */
export function refreshSession(): Promise<AuthResponse | null> {
  if (!refreshInFlight) {
    const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
    const run = locks ? locks.request("cc-auth-refresh", doRefresh) : doRefresh();
    refreshInFlight = run.finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** Send the access token and refresh on 401 (default true). */
  auth?: boolean;
  headers?: Record<string, string>;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, headers = {} } = options;

  const send = () => {
    const finalHeaders: Record<string, string> = { Accept: "application/json", ...headers };
    if (body !== undefined) finalHeaders["Content-Type"] = "application/json";
    if (auth && accessToken) finalHeaders.Authorization = `Bearer ${accessToken}`;
    return fetch(`${API_BASE}${path}`, {
      method,
      credentials: "include",
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  };

  let response = await send();
  if (response.status === 401 && auth) {
    const refreshed = await refreshSession();
    if (refreshed) {
      response = await send();
    } else {
      sessionExpiredListeners.forEach((listener) => listener());
    }
  }
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const authApi = {
  login: (email: string, password: string) =>
    apiFetch<AuthResponse>("/api/v1/auth/login", {
      method: "POST",
      body: { email, password },
      auth: false,
    }),
  register: (input: { email: string; password: string; full_name: string; company_name: string }) =>
    apiFetch<AuthResponse>("/api/v1/auth/register", { method: "POST", body: input, auth: false }),
  logout: () =>
    apiFetch<void>("/api/v1/auth/logout", { method: "POST", auth: false, headers: CSRF_HEADER }),
};
