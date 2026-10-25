import { describe, expect, it } from "vitest";
import { formatBytes } from "./App";

describe("file display helpers", () => {
  it("formats file sizes for the desktop table", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 * 1024)).toBe("1.0 MB");
  });
});
