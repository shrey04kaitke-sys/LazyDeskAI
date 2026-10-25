import { describe, expect, it } from "vitest";
import { resolveApiConfig } from "./apiConfig";
import { countsText, expiryNote, fallbackControl, moveLine, statusLabel } from "./applyFlow";
import type { PreviewMove } from "./applyFlow";

const move: PreviewMove = {
  action_id: "a1", source: "C:\ws\a.txt", destination: "C:\ws\documents\a.txt", current_name: "a.txt", new_name: "a.txt", renamed: false,
  category: "documents", folder: "C:\ws\documents", creates_folder: true, size: 3, name_source: "original",
};

describe("apply confirmation text", () => {
  it("shows the exact source and destination, and flags a rename", () => {
    expect(moveLine(move)).toBe("C:\ws\a.txt → C:\ws\documents\a.txt");
    expect(moveLine({ ...move, renamed: true, new_name: "b.txt" })).toContain("and renamed to b.txt");
  });
  it("tells the user the confirmation is short-lived and exact", () => {
    expect(expiryNote(120)).toContain("about 2 minutes");
    expect(expiryNote(30)).toContain("about a minute");
    expect(expiryNote(120)).toContain("if anything changes");
  });
});

describe("run history text", () => {
  it("uses honest status wording", () => {
    expect(statusLabel("completed")).toBe("Applied");
    expect(statusLabel("partial")).toBe("Partly applied");
    expect(statusLabel("needs_attention")).toBe("Needs attention");
    expect(statusLabel("undo_blocked")).toBe("Undo refused");
    expect(statusLabel("something-new")).toBe("something-new");
  });
  it("summarizes per-file states", () => {
    expect(countsText({ counts: { moved: 2, failed: 1, skipped: 0 } })).toBe("2 moved, 1 failed");
    expect(countsText({ counts: { undo_blocked: 1 } })).toBe("1 undo blocked");
  });
});

describe("explicit deterministic fallback (F7)", () => {
  it("is always selectable until it is actually saved", () => {
    expect(fallbackControl(null, false)).toEqual({ label: "Always use deterministic fallback", disabled: false });
    expect(fallbackControl("model", false).disabled).toBe(false);
    expect(fallbackControl("none", false)).toEqual({ label: "Deterministic fallback is saved", disabled: true });
    expect(fallbackControl(undefined, true).disabled).toBe(true);
  });
});

describe("backend port configuration", () => {
  it("defaults to 8000 and supports other valid ports", () => {
    expect(resolveApiConfig(undefined)).toEqual({ ok: true, base: "http://127.0.0.1:8000/api", port: 8000 });
    expect(resolveApiConfig("9123")).toEqual({ ok: true, base: "http://127.0.0.1:9123/api", port: 9123 });
    expect(resolveApiConfig(" 65535 ")).toMatchObject({ ok: true, port: 65535 });
  });
  it("rejects unsupported values with a clear message instead of guessing", () => {
    for (const bad of ["", "abc", "0", "65536", "-1", "80 80", "8000.5", "http://x", "8000; rm"]) {
      const result = resolveApiConfig(bad);
      expect(result.ok, bad).toBe(false);
      if (!result.ok) { expect(result.message).toContain("LAZYDESK_PORT"); expect(result.message).toContain("no request was sent"); }
    }
  });
  it("does not echo control characters or very long values", () => {
    const result = resolveApiConfig("\u0000\u001b[31m" + "x".repeat(200));
    expect(result.ok).toBe(false);
    if (!result.ok) { expect(result.message.length).toBeLessThan(250); expect(result.message).not.toContain("\u0000"); }
  });
});

describe("confirmation dialog contract (source-level: the test runner has no DOM)", () => {
  it("focuses Cancel by default, lists Cancel first, and Escape cancels", async () => {
    const source = (await import("./App.tsx?raw")).default as string;
    const dialog = source.slice(source.indexOf("function ConfirmDialog"), source.indexOf("function ConfirmDialog") + 1800);
    expect(dialog).toContain('autoFocus onClick={onCancel}');
    expect(dialog.indexOf("Cancel</button>")).toBeLessThan(dialog.indexOf("onClick={onConfirm}"));
    expect(dialog).toContain('event.key === "Escape"');
    expect(dialog).toContain('role="dialog"');
    expect(dialog.match(/autoFocus/g)).toHaveLength(1);
  });
  it("never calls /execute without a confirmation token, and previews first", async () => {
    const source = (await import("./App.tsx?raw")).default as string;
    expect(source.split('"/execute"')).toHaveLength(2);          // exactly one call site
    const call = source.slice(source.indexOf('"/execute"'), source.indexOf('"/execute"') + 400);
    expect(call).toContain("confirmation_token: preview.confirmation_token");
    expect(source).toContain("/preview");
  });
  it("sends the reviewed revisions when approving", async () => {
    const source = (await import("./App.tsx?raw")).default as string;
    expect(source).toContain("revisions }) });");
  });
});
