import { describe, expect, it } from "vitest";
import { formatBytes, incompleteScanMessage, scanSummary } from "./App";

describe("scan summary", () => {
  const processing = { cache_hits: 0, cache_misses: 3, model_requests: 2, model_fallbacks: 0 };
  it("reports counts and truncation", () => {
    expect(scanSummary(3, false, processing)).toBe("3 files discovered");
    expect(scanSummary(3, true, processing)).toContain("scan incomplete");
  });
  it("tells the user when the model fell back to rules", () => {
    expect(scanSummary(3, false, { ...processing, model_fallbacks: 2 })).toContain("2 file(s) used the built-in rules");
  });
});

describe("scan completeness warning", () => {
  const base = { scan_root: "C:\\Home", files_scanned: 10000, limit: 10000 };
  it("is empty for complete scans and missing metadata", () => {
    expect(incompleteScanMessage({ ...base, truncated: false, complete: true })).toBe("");
    expect(incompleteScanMessage(null)).toBe("");
  });
  it("says the scan was incomplete and names the root", () => {
    const message = incompleteScanMessage({ ...base, truncated: true, complete: false });
    expect(message).toContain("Incomplete scan");
    expect(message).toContain("C:\\Home");
    expect(message).toContain("10,000");
  });
});

describe("file display helpers", () => {
  it("formats file sizes for the desktop table", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
  });
});
