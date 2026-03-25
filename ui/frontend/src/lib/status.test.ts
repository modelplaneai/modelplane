import { describe, expect, test } from "vitest";
import { deriveStatus, statusText, conditionDotStatus } from "./status";
import type { Condition } from "../api/types";

describe("deriveStatus", () => {
  test.each([
    { name: "ready when Ready=True", input: [{ type: "Ready", status: "True" as const }], expected: "ready" },
    { name: "creating when reason=Creating", input: [{ type: "Ready", status: "False" as const, reason: "Creating" }], expected: "creating" },
    { name: "creating when reason=Pending", input: [{ type: "Ready", status: "False" as const, reason: "Pending" }], expected: "creating" },
    { name: "creating when reason=Progressing", input: [{ type: "Ready", status: "False" as const, reason: "Progressing" }], expected: "creating" },
    { name: "error when Ready=False with non-progressing reason", input: [{ type: "Ready", status: "False" as const, reason: "Failed" }], expected: "error" },
    { name: "error when Ready=False with no reason", input: [{ type: "Ready", status: "False" as const }], expected: "error" },
    { name: "unknown when conditions undefined", input: undefined, expected: "unknown" },
    { name: "unknown when conditions empty", input: [], expected: "unknown" },
    { name: "unknown when no Ready condition", input: [{ type: "Synced", status: "True" as const }], expected: "unknown" },
  ])("returns $expected ($name)", ({ input, expected }) => {
    expect(deriveStatus(input as Condition[] | undefined)).toBe(expected);
  });
});

describe("statusText", () => {
  test.each([
    { name: "Ready when Ready=True", input: [{ type: "Ready", status: "True" as const }], expected: "Ready" },
    { name: "the reason when Ready=False with reason", input: [{ type: "Ready", status: "False" as const, reason: "Creating" }], expected: "Creating" },
    { name: "Error when Ready=False with no reason", input: [{ type: "Ready", status: "False" as const }], expected: "Error" },
    { name: "Unknown when conditions undefined", input: undefined, expected: "Unknown" },
    { name: "Unknown when conditions empty", input: [], expected: "Unknown" },
    { name: "Unknown when no Ready condition", input: [{ type: "Synced", status: "True" as const }], expected: "Unknown" },
  ])("returns $expected ($name)", ({ input, expected }) => {
    expect(statusText(input as Condition[] | undefined)).toBe(expected);
  });
});

describe("conditionDotStatus", () => {
  test.each([
    { name: "ready when status=True", input: { type: "Ready", status: "True" as const }, expected: "ready" },
    { name: "error when status=False", input: { type: "Ready", status: "False" as const }, expected: "error" },
    { name: "unknown when status=Unknown", input: { type: "Ready", status: "Unknown" as const }, expected: "unknown" },
  ])("returns $expected ($name)", ({ input, expected }) => {
    expect(conditionDotStatus(input)).toBe(expected);
  });
});
