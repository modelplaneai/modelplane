import { describe, expect, test, vi, afterEach } from "vitest";
import { modelDisplayName, relativeAge, isValidKubernetesName } from "./format";

describe("modelDisplayName", () => {
  test.each([
    { input: "Qwen/Qwen2.5-0.5B-Instruct", expected: "Qwen 2.5 0.5B Instruct" },
    { input: "meta-llama/Llama-3.1-70B-Instruct", expected: "Llama 3.1 70B Instruct" },
    { input: "simple-model", expected: "simple model" },
    { input: "org/name_with_underscores", expected: "name with underscores" },
    { input: "no-slash", expected: "no slash" },
  ])("converts '$input' to '$expected'", ({ input, expected }) => {
    expect(modelDisplayName(input)).toBe(expected);
  });
});

describe("relativeAge", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  test("returns dash when timestamp is undefined", () => {
    expect(relativeAge(undefined)).toBe("—");
  });

  test.each([
    { name: "just now", offsetMs: 30_000, expected: "just now" },
    { name: "minutes ago", offsetMs: 5 * 60_000, expected: "5m ago" },
    { name: "hours ago", offsetMs: 3 * 3_600_000, expected: "3h ago" },
    { name: "days ago", offsetMs: 2 * 86_400_000, expected: "2d ago" },
  ])("returns '$expected' ($name)", ({ offsetMs, expected }) => {
    const now = new Date("2026-03-24T12:00:00Z").getTime();
    vi.useFakeTimers();
    vi.setSystemTime(now);

    const ts = new Date(now - offsetMs).toISOString();
    expect(relativeAge(ts)).toBe(expected);
  });
});

describe("isValidKubernetesName", () => {
  test.each([
    { input: "valid-name", expected: true },
    { input: "a", expected: true },
    { input: "my-resource-123", expected: true },
    { input: "a1b2", expected: true },
    { input: "Invalid", expected: false },
    { input: "-starts-with-dash", expected: false },
    { input: "ends-with-dash-", expected: false },
    { input: "", expected: false },
    { input: "has spaces", expected: false },
    { input: "has_underscore", expected: false },
    { input: "ALLCAPS", expected: false },
    { input: "has.dot", expected: false },
  ])("validates '$input' as $expected", ({ input, expected }) => {
    expect(isValidKubernetesName(input)).toBe(expected);
  });
});
