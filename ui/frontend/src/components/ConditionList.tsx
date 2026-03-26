import { StatusDot } from "./StatusDot";
import { conditionDotStatus } from "../lib/status";
import type { Condition } from "../api/types";

interface ConditionListProps {
  conditions: Condition[];
}

export function ConditionList({ conditions }: ConditionListProps) {
  if (conditions.length === 0) {
    return <p className="text-sm text-muted">No conditions reported</p>;
  }

  return (
    <ul className="space-y-1">
      {conditions.map((c) => (
        <li key={c.type} className="flex items-center gap-2 text-sm">
          <StatusDot status={conditionDotStatus(c)} />
          <span className="text-text">{c.type}</span>
          {c.reason && (
            <span className="text-muted-hi">— {c.reason}</span>
          )}
          {c.message && (
            <span className="text-muted truncate" title={c.message}>
              {c.message}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
