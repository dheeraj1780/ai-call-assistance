import { beforeEach, describe, expect, it, vi } from "vitest";

type ApiModule = typeof import("./api");

const authBody = {
  access_token: "new-access",
  token_type: "bearer",
  expires_in: 900,
  user: { id: "u1", email: "a@example.com", full_name: "A", phone: null },
  company: { id: "c1", name: "Co" },
  role: "OWNER",
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

let api: ApiModule;
const fetchMock = vi.fn<typeof fetch>();

beforeEach(async () => {
  vi.resetModules();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  api = await import("./api");
});

describe("apiFetch", () => {
  it("sends the bearer token and includes credentials", async () => {
    api.setAccessToken("tok");
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));

    await expect(api.apiFetch("/api/v1/me")).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/me");
    expect(init?.credentials).toBe("include");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    api.setAccessToken("expired");
    fetchMock
      .mockResolvedValueOnce(json(401, { error: { code: "invalid_token", message: "x" } }))
      .mockResolvedValueOnce(json(200, authBody))
      .mockResolvedValueOnce(json(200, { ok: true }));

    await expect(api.apiFetch("/api/v1/me")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const [refreshUrl, refreshInit] = fetchMock.mock.calls[1]!;
    expect(refreshUrl).toBe("/api/v1/auth/refresh");
    expect((refreshInit?.headers as Record<string, string>)["X-CSRF-Protection"]).toBe("1");
    const retryHeaders = fetchMock.mock.calls[2]![1]?.headers as Record<string, string>;
    expect(retryHeaders.Authorization).toBe("Bearer new-access");
  });

  it("notifies listeners and throws when the session cannot be refreshed", async () => {
    const expired = vi.fn();
    api.onSessionExpired(expired);
    fetchMock
      .mockResolvedValueOnce(json(401, { error: { code: "invalid_token", message: "Expired" } }))
      .mockResolvedValueOnce(json(401, { error: { code: "invalid_refresh_token", message: "x" } }));

    await expect(api.apiFetch("/api/v1/me")).rejects.toMatchObject({
      status: 401,
      code: "invalid_token",
    });
    expect(expired).toHaveBeenCalledOnce();
  });

  it("parses the uniform error schema", async () => {
    fetchMock.mockResolvedValueOnce(
      json(409, {
        error: { code: "email_taken", message: "Exists", details: null, request_id: "r1" },
      }),
    );
    const error = await api.apiFetch("/x", { auth: false }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(api.ApiError);
    expect(error).toMatchObject({ status: 409, code: "email_taken", requestId: "r1", message: "Exists" });
  });

  it("does not attempt a refresh for unauthenticated calls", async () => {
    fetchMock.mockResolvedValueOnce(json(401, { error: { code: "invalid_credentials", message: "Bad" } }));
    await expect(api.authApi.login("a@example.com", "pw")).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("refreshSession", () => {
  it("coalesces concurrent refreshes into one request (refresh tokens rotate)", async () => {
    fetchMock.mockResolvedValue(json(200, authBody));
    const [a, b] = await Promise.all([api.refreshSession(), api.refreshSession()]);
    expect(a).toEqual(authBody);
    expect(b).toEqual(authBody);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns null when there is no session", async () => {
    fetchMock.mockResolvedValueOnce(json(401, { error: { code: "invalid_refresh_token" } }));
    await expect(api.refreshSession()).resolves.toBeNull();
  });
});
