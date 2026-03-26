import { relativeAge } from "../lib/format";
import type { KubeEvent } from "../api/types";

interface EventTimelineProps {
  events: KubeEvent[];
}

export function EventTimeline({ events }: EventTimelineProps) {
  if (events.length === 0) {
    return <p className="text-sm text-muted">No events</p>;
  }

  // Sort newest-first by lastTimestamp.
  const sorted = [...events].sort(
    (a, b) =>
      new Date(b.lastTimestamp ?? "").getTime() -
      new Date(a.lastTimestamp ?? "").getTime(),
  );

  return (
    <ul className="space-y-1.5">
      {sorted.map((ev, i) => (
        <li key={`${ev.metadata.name}-${i}`} className="flex items-start gap-2 text-sm">
          <span
            className={`mt-1 inline-block w-1.5 h-1.5 rounded-full shrink-0 ${
              ev.type === "Warning" ? "bg-red" : "bg-muted"
            }`}
          />
          <span className="text-muted-hi flex-1">{ev.message}</span>
          <span className="text-muted text-xs shrink-0 tabular-nums">
            {relativeAge(ev.lastTimestamp)}
          </span>
        </li>
      ))}
    </ul>
  );
}
