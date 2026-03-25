import type { Condition } from "../api/types";

export type StatusLevel = "ready" | "creating" | "error" | "unknown";

// deriveStatus maps a Kubernetes conditions array to a display status.
export function deriveStatus(conditions?: Condition[]): StatusLevel {
  if (!conditions || conditions.length === 0) return "unknown";
  const ready = conditions.find((c) => c.type === "Ready");
  if (!ready) return "unknown";
  if (ready.status === "True") return "ready";
  if (ready.reason === "Creating" || ready.reason === "Pending" || ready.reason === "Progressing") return "creating";
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

// conditionDotStatus maps a single condition to a status level.
export function conditionDotStatus(c: Condition): StatusLevel {
  if (c.status === "True") return "ready";
  if (c.status === "False") return "error";
  return "unknown";
}
