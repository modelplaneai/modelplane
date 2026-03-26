// Labels renders the user-meaningful labels on a Kubernetes resource.
// Crossplane-internal labels (crossplane.io/*) are hidden. Modelplane
// labels (modelplane.ai/*) have their prefix stripped for readability.

interface LabelsProps {
  labels?: Record<string, string>;
  className?: string;
}

const HIDDEN_PREFIXES = ["crossplane.io/"];

function isHidden(key: string): boolean {
  return HIDDEN_PREFIXES.some((p) => key.startsWith(p));
}

export function filterLabels(
  labels?: Record<string, string>,
): [string, string][] {
  if (!labels) return [];
  return Object.entries(labels).filter(([k]) => !isHidden(k));
}

export function Labels({ labels, className }: LabelsProps) {
  const filtered = filterLabels(labels);
  if (filtered.length === 0) return null;

  return (
    <div className={`flex flex-wrap gap-1.5 ${className ?? ""}`}>
      {filtered.map(([k, v]) => (
        <span
          key={k}
          className="text-xs font-mono text-muted-hi bg-bg-mid px-2 py-0.5 rounded-md border border-border"
        >
          {k}: {v}
        </span>
      ))}
    </div>
  );
}
