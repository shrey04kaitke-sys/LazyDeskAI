import { describe, expect, it } from "vitest";
import { ApiError, MESSAGES, TOKEN_HEADER, createApiClient } from "./apiClient";

const BASE = "http://127.0.0.1:8000/api";
type Call = { url: string; init: RequestInit };

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** A fake backend: hands out `tokens` in order from bootstrap and only accepts the currently valid one. */
function backend(tokens: string[], options: { bootstrapStatus?: number } = {}) {
  const calls: Call[] = [];
  let issued = -1;
  const fetchImpl = async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url === `${BASE}/session/bootstrap`) {
      if (options.bootstrapStatus) return json(options.bootstrapStatus, { detail: { message: "Request rejected.", code: "request_rejected" } });
      issued = Math.min(issued + 1, tokens.length - 1);
      return json(200, { token: tokens[issued], header: TOKEN_HEADER });
    }
    const sent = (init.headers as Record<string, string>)[TOKEN_HEADER];
    if (sent !== tokens[issued]) return json(401, { detail: { message: "Authorization required.", code: "authorization_required" } });
    return json(200, { ok: true, path: url.replace(BASE, "") });
  };
  return { calls, fetchImpl };
}

describe("api client authorization flow", () => {
  it("bootstraps once, then attaches the token to every request", async () => {
    const { calls, fetchImpl } = backend(["t-1"]);
    const client = createApiClient(BASE, fetchImpl);
    expect(client.hasToken()).toBe(false);
    await client.request("/scan", { method: "POST", body: "{}" });
    await client.request("/activity");
    expect(calls.filter((c) => c.url.endsWith("/session/bootstrap"))).toHaveLength(1);
    const sent = calls.filter((c) => !c.url.endsWith("/session/bootstrap"));
    expect(sent.map((c) => (c.init.headers as Record<string, string>)[TOKEN_HEADER])).toEqual(["t-1", "t-1"]);
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.cache).toBe("no-store");
  });

  it("never puts the token in a URL or body", async () => {
    const { calls, fetchImpl } = backend(["secret-token-value"]);
    const client = createApiClient(BASE, fetchImpl);
    await client.request("/plans", { method: "POST", body: JSON.stringify({ a: 1 }) });
    for (const call of calls) {
      expect(call.url).not.toContain("secret-token-value");
      expect(String(call.init.body ?? "")).not.toContain("secret-token-value");
    }
  });

  it("shares one bootstrap between concurrent first requests", async () => {
    const { calls, fetchImpl } = backend(["t-1"]);
    const client = createApiClient(BASE, fetchImpl);
    await Promise.all([client.request("/a"), client.request("/b"), client.request("/c")]);
    expect(calls.filter((c) => c.url.endsWith("/session/bootstrap"))).toHaveLength(1);
  });

  it("re-bootstraps once when the backend was restarted, then succeeds", async () => {
    let valid = "old";
    const calls: string[] = [];
    const fetchImpl = async (url: string, init: RequestInit = {}) => {
      calls.push(url.replace(BASE, ""));
      if (url.endsWith("/session/bootstrap")) return json(200, { token: valid });
      return (init.headers as Record<string, string>)[TOKEN_HEADER] === valid ? json(200, { ok: true }) : json(401, { detail: { code: "authorization_required" } });
    };
    const client = createApiClient(BASE, fetchImpl);
    await client.request("/activity");
    valid = "new";  // the backend restarted and rotated its token
    await expect(client.request("/activity")).resolves.toEqual({ ok: true });
    expect(calls).toEqual(["/session/bootstrap", "/activity", "/activity", "/session/bootstrap", "/activity"]);
  });

  it("gives a clear error, without retrying forever, when authorization keeps failing", async () => {
    const calls: string[] = [];
    const fetchImpl = async (url: string) => {
      calls.push(url);
      return url.endsWith("/session/bootstrap") ? json(200, { token: "never-valid" }) : json(401, { detail: { message: "Authorization required.", code: "authorization_required" } });
    };
    const client = createApiClient(BASE, fetchImpl);
    const error = (await client.request("/execute", { method: "POST", body: "{}" }).catch((e) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe(MESSAGES.unauthorized);
    expect(error.message).not.toContain("never-valid");
    expect(calls.filter((u) => !u.endsWith("/session/bootstrap"))).toHaveLength(2);  // original + one retry
    expect(client.hasToken()).toBe(false);
  });

  it("does not send the real request when bootstrap is refused, and says why", async () => {
    const { calls, fetchImpl } = backend(["t"], { bootstrapStatus: 403 });
    const client = createApiClient(BASE, fetchImpl);
    const error = (await client.request("/execute", { method: "POST", body: "{}" }).catch((e) => e)) as ApiError;
    expect(error.message).toBe(MESSAGES.origin);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`${BASE}/session/bootstrap`);
  });

  it("never falls back to an unauthenticated request when the token is malformed", async () => {
    const calls: string[] = [];
    const client = createApiClient(BASE, async (url) => { calls.push(url); return json(200, { token: 42 }); });
    await expect(client.request("/scan", { method: "POST", body: "{}" })).rejects.toThrow(MESSAGES.unauthorized);
    expect(calls).toEqual([`${BASE}/session/bootstrap`]);
  });

  it("reports an unreachable backend without leaking details", async () => {
    const client = createApiClient(BASE, async () => { throw new TypeError("Failed to fetch"); });
    await expect(client.request("/scan")).rejects.toThrow(MESSAGES.unreachable);
  });

  it("keeps meaningful server errors, and lets a later bootstrap succeed after a failed one", async () => {
    let allow = false;
    const fetchImpl = async (url: string, init: RequestInit = {}) => {
      if (url.endsWith("/session/bootstrap")) return allow ? json(200, { token: "t" }) : json(403, { detail: "no" });
      return (init.headers as Record<string, string>)[TOKEN_HEADER] === "t"
        ? json(403, { detail: { message: "Folder is protected.", code: "credentials" } }) : json(401, {});
    };
    const client = createApiClient(BASE, fetchImpl);
    await expect(client.request("/scan")).rejects.toThrow(MESSAGES.origin);
    allow = true;
    const error = (await client.request("/scan").catch((e) => e)) as ApiError;
    expect(error.message).toBe("Folder is protected.");
    expect(error.code).toBe("credentials");
  });
});
