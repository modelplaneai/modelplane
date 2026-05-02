// modelDisplayName turns a model identifier like "Qwen/Qwen2.5-0.5B-Instruct"
// into a human-readable name like "Qwen2.5 0.5B Instruct".
export function modelDisplayName(name: string): string {
  const parts = name.split("/");
  const last = parts[parts.length - 1];
  return last.replace(/[-_]/g, " ").replace(/([a-z])(\d)/gi, "$1 $2");
}

// relativeAge turns an ISO timestamp into a human-readable relative time.
export function relativeAge(ts?: string): string {
  if (!ts) return "—";
  const diff = Date.now() - new Date(ts).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// isValidKubernetesName checks whether a string is a valid Kubernetes resource
// name: lowercase alphanumeric, hyphens, max 253 chars, must start and end
// with an alphanumeric character.
export function isValidKubernetesName(name: string): boolean {
  return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(name) && name.length <= 253;
}

// toKubernetesName sanitizes a string into a valid Kubernetes name by
// lowercasing, replacing non-alphanumeric runs with hyphens, and trimming
// leading/trailing hyphens.
export function toKubernetesName(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

// envRegion extracts the region from an InferenceEnvironment's labels.
export function envRegion(env: { metadata: { labels?: Record<string, string> } }): string | undefined {
  return env.metadata.labels?.["modelplane.ai/region"];
}

// envClusterSource extracts the cluster source from an InferenceEnvironment.
export function envClusterSource(env: { spec: { cluster?: { source: string } } }): string | undefined {
  return env.spec.cluster?.source;
}
