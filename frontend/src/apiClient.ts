// Talks to the local LazyDesk backend. The per-launch token is fetched once from /session/bootstrap and kept ONLY in this
// closure (JavaScript memory): it is never written to storage, cookies, URLs or logs, and every request goes through
// `request`, so there is no way to send an unauthenticated mutating call from the app.
export const TOKEN_HEADER = "X-LazyDesk-Token";

export class ApiError extends Error {
  constructor(message: string, readonly status = 0, readonly code = "") {
    super(message);
    this.name = "ApiError";
  }
}

export const MESSAGES = {
  unreachable: "LazyDesk cannot reach its local backend. Start the backend and reload this page.",
  origin: "The local backend refused this page. Open LazyDesk at http://127.0.0.1:5173 and reload.",
  unauthorized: "LazyDesk could not authorize with its local backend (it may have been restarted). Reload this page; nothing was changed.",
  generic: "The local API request failed.",
} as const;

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

async function readDetail(response: Response): Promise<{ message: string; code: string }> {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (typeof detail === "string") return { message: detail, code: "" };
    return { message: typeof detail?.message === "string" ? detail.message : MESSAGES.generic, code: typeof detail?.code === "string" ? detail.code : "" };
  } catch {
    return { message: MESSAGES.generic, code: "" };
  }
}

export function createApiClient(base: string, fetchImpl: FetchLike = (input, init) => fetch(input, init)) {
  let token: string | null = null;
  let pending: Promise<string> | null = null;

  async function fetchToken(): Promise<string> {
    let response: Response;
    try {
      response = await fetchImpl(`${base}/session/bootstrap`, { method: "POST", cache: "no-store" });
    } catch {
      throw new ApiError(MESSAGES.unreachable);
    }
    if (!response.ok) throw new ApiError(response.status === 403 || response.status === 400 ? MESSAGES.origin : MESSAGES.unauthorized, response.status);
    let body: { token?: unknown };
    try { body = await response.json(); } catch { throw new ApiError(MESSAGES.unauthorized, response.status); }
    if (typeof body.token !== "string" || !body.token) throw new ApiError(MESSAGES.unauthorized, response.status);
    return body.token;
  }

  function bootstrap(): Promise<string> {
    if (!pending) {
      const attempt = fetchToken();
      pending = attempt;
      const clear = () => { pending = null; };
      attempt.then(clear, clear);
    }
    return pending;
  }

  async function request<T>(path: string, options: RequestInit = {}, retried = false): Promise<T> {
    if (!token) token = await bootstrap();
    let response: Response;
    try {
      response = await fetchImpl(`${base}${path}`, { ...options, headers: { "Content-Type": "application/json", [TOKEN_HEADER]: token } });
    } catch {
      throw new ApiError(MESSAGES.unreachable);
    }
    if (response.status === 401) {
      token = null;  // expired or from an earlier backend launch: fetch a fresh one once, then give up clearly
      if (!retried) return request<T>(path, options, true);
      throw new ApiError(MESSAGES.unauthorized, 401, "authorization_required");
    }
    if (!response.ok) {
      const { message, code } = await readDetail(response);
      throw new ApiError(message, response.status, code);
    }
    return (await response.json()) as T;
  }

  return { request, hasToken: () => token !== null };
}
