// The backend port the UI talks to. It comes from LAZYDESK_PORT at dev/build time (default 8000) and must be the same value the
// backend was started with. An unsupported value is rejected here, so the UI makes NO request instead of guessing a port.
export type ApiConfig = { ok: true; base: string; port: number } | { ok: false; message: string };

export function resolveApiConfig(raw: string | undefined): ApiConfig {
  const text = (raw ?? "8000").trim();
  const port = Number(text);
  if (!/^[0-9]{1,5}$/.test(text) || port < 1 || port > 65535) {
    const shown = JSON.stringify(text.replace(/[^\x20-\x7e]/g, "?").slice(0, 20));
    return { ok: false, message: `LazyDesk's backend port setting (LAZYDESK_PORT = ${shown}) is not a whole number from 1 to 65535. Fix it and restart the frontend; no request was sent.` };
  }
  return { ok: true, base: `http://127.0.0.1:${port}/api`, port };
}
