import type { Condition } from "../api/types";

export type StatusLevel = "ready" | "creating" | "error" | "unknown";

// Reasons that indicate work in progress rather than failure.
const PROGRESS_REASONS = new Set([
  "Creating",
  "Pending",
  "Progressing",
  "Provisioning",
  "Installing",
  "WaitingForCluster",
  "WaitingForController",
  "WaitingForAddress",
  "WaitingForReferences",
  "WaitingForEnvironment",
  "WaitingForModel",
  "WaitingForPlacements",
  "Deploying",
  "ModelStarting",
  "Scheduling",
  "Available", // Crossplane sets reason=Available before Ready=True.
]);

function isProgressReason(reason?: string): boolean {
  return !!reason && PROGRESS_REASONS.has(reason);
}

// deriveStatus maps a Kubernetes conditions array to a display status.
export function deriveStatus(conditions?: Condition[]): StatusLevel {
  if (!conditions || conditions.length === 0) return "unknown";
  const ready = conditions.find((c) => c.type === "Ready");
  if (!ready) return "unknown";
  if (ready.status === "True") return "ready";
  if (isProgressReason(ready.reason)) return "creating";
  if (ready.status === "False") return "error";
  return "unknown";
}

// statusText returns a human-readable status string from conditions.
export function statusText(conditions?: Condition[]): string {
  if (!conditions || conditions.length === 0) return "Unknown";
  const ready = conditions.find((c) => c.type === "Ready");
  if (!ready) return "Unknown";
  if (ready.status === "True") return "Ready";
  if (ready.reason) return ready.reason;
  if (ready.status === "False") return "Error";
  return "Unknown";
}

// conditionDotStatus maps a single condition to a status level. Conditions
// that are False with a progress-like reason show as "creating" (amber pulse)
// rather than "error" (red).
export function conditionDotStatus(c: Condition): StatusLevel {
  if (c.status === "True") return "ready";
  if (c.status === "False" && isProgressReason(c.reason)) return "creating";
  if (c.status === "False") return "error";
  return "unknown";
}
