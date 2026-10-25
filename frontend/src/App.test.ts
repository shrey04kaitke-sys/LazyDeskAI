import { describe, expect, it } from "vitest";
import { describeBasis, describeModelChoice, formatBytes, incompleteScanMessage, isLegacyPlan, nameSourceLabel, scanSummary, skippedFoldersMessage } from "./App";

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

describe("skipped protected folders notice", () => {
  const base = { scan_root: "C:\\Home", files_scanned: 3, limit: 10000, truncated: false, complete: true };
  it("is empty when nothing was skipped or the metadata is missing", () => {
    expect(skippedFoldersMessage(null)).toBe("");
    expect(skippedFoldersMessage(base)).toBe("");
    expect(skippedFoldersMessage({ ...base, skipped_protected: [], skipped_protected_total: 0 })).toBe("");
  });
  it("names skipped folders and why", () => {
    const message = skippedFoldersMessage({ ...base, skipped_protected: [{ path: ".ssh", reason: "credentials folder" }], skipped_protected_total: 1 });
    expect(message).toContain("1 protected folder was skipped");
    expect(message).toContain(".ssh (credentials folder)");
  });
  it("counts folders beyond the reported list", () => {
    const message = skippedFoldersMessage({ ...base, skipped_protected: [{ path: "a/.ssh", reason: "credentials folder" }], skipped_protected_total: 4 });
    expect(message).toContain("4 protected folders were skipped");
    expect(message).toContain("and 3 more");
  });
});

describe("file display helpers", () => {
  it("formats file sizes for the desktop table", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
  });
});


describe("model explanations are labelled as unverified filename-and-size inference", () => {
  it("describes rule-based proposals without any model wording", () => {
    const basis = describeBasis({ explanation_basis: "extension-rule", model: null, model_note: null });
    expect(basis.source).toBe("Local extension rule");
    expect(basis.note).toBeNull();
  });
  it("states the basis and quotes the model's note as unverified", () => {
    const basis = describeBasis({ explanation_basis: "filename-and-size", model: "qwen2.5:3b", model_note: "Looks like a tax return." });
    expect(basis.source).toContain("qwen2.5:3b");
    expect(basis.source).toContain("file name and size only");
    expect(basis.source).toContain("contents were not read");
    expect(basis.note).toContain("unverified");
    expect(basis.note).toContain("Looks like a tax return.");
  });
  it("labels where a name came from", () => {
    expect(nameSourceLabel("original")).toBe("original file name");
    expect(nameSourceLabel("user")).toBe("edited by you");
    expect(nameSourceLabel("model-suggestion")).toContain("applied by you");
  });
});

describe("scan summary explains cold-start fallbacks", () => {
  it("says the model did not load in time when the warm-up failed", () => {
    const text = scanSummary(5, false, { cache_hits: 0, cache_misses: 5, model_requests: 0, model_fallbacks: 5, model_warmup_failures: 1 });
    expect(text).toContain("did not load in time");
  });
});

describe("legacy plans are identified", () => {
  it("treats missing or old schema numbers as legacy and the current one as current", () => {
    expect(isLegacyPlan({})).toBe(true);
    expect(isLegacyPlan({ plan_schema: 0 })).toBe(true);
    expect(isLegacyPlan({ plan_schema: 2 })).toBe(false);
  });
});

describe("the Settings page explains where the model choice comes from", () => {
  it("says so when no model is used", () => {
    expect(describeModelChoice(null, "none", false)).toContain("built-in rules only");
  });
  it("names the built-in default and whether it is installed", () => {
    expect(describeModelChoice("qwen2.5:3b", "default", true)).toContain("built-in default");
    expect(describeModelChoice("qwen2.5:3b", "default", false)).toContain("not installed");
  });
  it("distinguishes explicit choices", () => {
    expect(describeModelChoice("m1", "setting", true)).toContain("your saved choice");
    expect(describeModelChoice("m1", "environment", true)).toContain("OLLAMA_MODEL");
  });
});
