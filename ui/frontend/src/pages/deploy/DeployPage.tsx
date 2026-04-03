import { useState, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useModels } from "../../hooks/useModels";
import { useEnvironments } from "../../hooks/useEnvironments";
import { useNamespace } from "../../hooks/useNamespace";
import { useApi } from "../../api/context";
import { deriveStatus } from "../../lib/status";
import { modelDisplayName, isValidKubernetesName, toKubernetesName, envRegion } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Labels, filterLabels } from "../../components/Labels";
import type { ClusterModel, InferenceEnvironment } from "../../api/types";

// Collect all unique filtered label key=value pairs from a list of resources.
function collectLabels(resources: Array<{ metadata: { labels?: Record<string, string> } }>): Map<string, Set<string>> {
  const result = new Map<string, Set<string>>();
  for (const r of resources) {
    for (const [k, v] of filterLabels(r.metadata.labels)) {
      if (!result.has(k)) result.set(k, new Set());
      result.get(k)!.add(v);
    }
  }
  return result;
}

// Filter resources by selected labels (using filtered/humanized keys).
function matchesLabels(
  labels: Record<string, string> | undefined,
  selected: Record<string, string>,
): boolean {
  const filtered = Object.fromEntries(filterLabels(labels));
  return Object.entries(selected).every(([k, v]) => filtered[k] === v);
}

export function DeployPage() {
  const navigate = useNavigate();
  const api = useApi();
  const { namespace } = useNamespace();
  const { data: modelsData, isLoading: modelsLoading } = useModels();
  const { data: envsData } = useEnvironments();

  const models = modelsData?.items ?? [];
  const environments = envsData?.items ?? [];

  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [deploymentName, setDeploymentName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const [modelLabelFilter, setModelLabelFilter] = useState<Record<string, string>>({});
  const [envLabelFilter, setEnvLabelFilter] = useState<Record<string, string>>({});
  const [envCount, setEnvCount] = useState("1");
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selected = models.find((m) => m.metadata.name === selectedModel);

  // Derive deployment name from model selection.
  const derivedName = selected
    ? toKubernetesName(`${selected.metadata.name}-deployment`)
    : "";

  const displayName = nameEdited ? deploymentName : derivedName;
  const nameInvalid = displayName !== "" && !isValidKubernetesName(displayName);

  // Model label chips.
  const allModelLabels = useMemo(() => collectLabels(models), [models]);

  // Filtered models.
  const matchingModels = useMemo(
    () => models.filter((m) => matchesLabels(m.metadata.labels, modelLabelFilter)),
    [models, modelLabelFilter],
  );

  // Environment label chips.
  const allEnvLabels = useMemo(() => collectLabels(environments), [environments]);

  // Filtered environments. Uses raw labels for the selector since the API
  // expects the original label keys, not the humanized ones.
  const matchingEnvs = useMemo(
    () => environments.filter((e) => matchesLabels(e.metadata.labels, envLabelFilter)),
    [environments, envLabelFilter],
  );

  const parsedEnvCount = Math.max(1, parseInt(envCount) || 1);

  function toggleLabel(
    filter: Record<string, string>,
    setFilter: (f: Record<string, string>) => void,
    key: string,
    value: string,
  ) {
    const next = { ...filter };
    if (next[key] === value) {
      delete next[key];
    } else {
      next[key] = value;
    }
    setFilter(next);
  }

  function selectModel(name: string) {
    setSelectedModel(name);
    setNameEdited(false);
    setEnvLabelFilter({});
    setError(null);
  }

  function buildEnvSelector(): Record<string, string> | undefined {
    if (Object.keys(envLabelFilter).length === 0) return undefined;
    return { ...envLabelFilter };
  }

  async function handleDeploy() {
    if (!selected) return;
    setDeploying(true);
    setError(null);

    const name = nameEdited ? deploymentName : derivedName;
    const matchLabels = buildEnvSelector();

    try {
      await api.createModelDeployment(namespace, {
        apiVersion: "modelplane.ai/v1alpha1",
        kind: "ModelDeployment",
        metadata: { name, namespace },
        spec: {
          modelRef: { kind: "ClusterModel", name: selected.metadata.name },
          environments: parsedEnvCount,
          ...(matchLabels && {
            environmentSelector: { matchLabels },
          }),
        },
      });
      navigate(`/deployments/${namespace}/${name}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Deployment failed");
      setDeploying(false);
    }
  }

  if (modelsLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading models…</span>
      </div>
    );
  }

  return (
    <div className="space-y-8 max-w-5xl">
      {/* Model selection */}
      <div>
        <SectionLabel>SELECT A MODEL</SectionLabel>

        {/* Model label filter chips */}
        {allModelLabels.size > 0 && (
          <div className="mb-4">
            <LabelChips
              allLabels={allModelLabels}
              selected={modelLabelFilter}
              onToggle={(k, v) => toggleLabel(modelLabelFilter, setModelLabelFilter, k, v)}
            />
          </div>
        )}

        {models.length === 0 ? (
          <p className="text-muted text-sm">No models found in the cluster.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {models.map((model) => (
              <ModelCard
                key={model.metadata.name}
                model={model}
                selected={model.metadata.name === selectedModel}
                dimmed={!matchingModels.includes(model)}
                onSelect={() => selectModel(model.metadata.name)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Configuration — revealed after selecting a model */}
      {selected && (
        <>
          {/* Deployment name */}
          <div>
            <SectionLabel>DEPLOYMENT NAME</SectionLabel>
            <input
              type="text"
              value={displayName}
              onChange={(e) => {
                setDeploymentName(e.target.value);
                setNameEdited(true);
              }}
              className="w-full max-w-md bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
            />
            {nameInvalid && (
              <p className="text-xs text-red mt-1">
                Must be lowercase alphanumeric or hyphens, and start/end with
                an alphanumeric character.
              </p>
            )}
          </div>

          {/* Environments */}
          <div>
            <SectionLabel>ENVIRONMENTS</SectionLabel>

            {/* Environment label filter chips */}
            {allEnvLabels.size > 0 && (
              <div className="mb-4">
                <LabelChips
                  allLabels={allEnvLabels}
                  selected={envLabelFilter}
                  onToggle={(k, v) => toggleLabel(envLabelFilter, setEnvLabelFilter, k, v)}
                />
              </div>
            )}

            {/* Environment cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {environments.map((env) => (
                <EnvironmentCard
                  key={env.metadata.name}
                  env={env}
                  dimmed={!matchingEnvs.includes(env)}
                />
              ))}
            </div>

            {/* Environment count */}
            <div className="mt-4 flex items-center gap-3">
              <label className="text-sm text-muted">Deploy to</label>
              <input
                type="number"
                min={1}
                max={matchingEnvs.length || 1}
                value={envCount}
                onChange={(e) => setEnvCount(e.target.value)}
                onBlur={() =>
                  setEnvCount(String(Math.max(1, parseInt(envCount) || 1)))
                }
                className="w-20 bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
              />
              <span className="text-sm text-muted">
                of {matchingEnvs.length} matching environment
                {matchingEnvs.length !== 1 ? "s" : ""}
              </span>
            </div>
            {parsedEnvCount < matchingEnvs.length && (
              <p className="text-xs text-muted mt-1">
                Modelplane will select {parsedEnvCount} of the{" "}
                {matchingEnvs.length} matching environments based on available
                capacity.
              </p>
            )}
          </div>

          {/* Error */}
          {error && <p className="text-sm text-red">{error}</p>}

          {/* Actions */}
          <div className="flex gap-3">
            <Button
              onClick={handleDeploy}
              disabled={deploying || nameInvalid || !displayName}
            >
              {deploying ? "Deploying…" : "Deploy"}
            </Button>
            <Button variant="ghost" onClick={() => navigate(-1)}>
              Cancel
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

// Shared label chip filter component.
function LabelChips({
  allLabels,
  selected,
  onToggle,
}: {
  allLabels: Map<string, Set<string>>;
  selected: Record<string, string>;
  onToggle: (key: string, value: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {[...allLabels.entries()].flatMap(([key, values]) =>
        [...values].map((value) => {
          const isActive = selected[key] === value;
          return (
            <button
              key={`${key}=${value}`}
              onClick={() => onToggle(key, value)}
              className={`text-xs font-mono px-2 py-1 rounded-md border transition ${
                isActive
                  ? "border-purple bg-purple/10 text-purple"
                  : "border-border text-muted hover:text-muted-hi hover:border-border-hi"
              }`}
            >
              {key}: {value}
            </button>
          );
        }),
      )}
    </div>
  );
}

function ModelCard({
  model,
  selected,
  dimmed,
  onSelect,
}: {
  model: ClusterModel;
  selected: boolean;
  dimmed: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      onClick={onSelect}
      className={`text-left rounded-xl transition ${
        dimmed ? "opacity-30" : ""
      } ${
        selected
          ? "ring-2 ring-purple"
          : "hover:ring-1 hover:ring-border-hi"
      }`}
    >
      <Card>
        <div className="space-y-2">
          <h3 className="text-text font-medium">
            {modelDisplayName(model.spec.model.name)}
          </h3>
          <div className="flex flex-wrap gap-2">
            {(model.spec.serving ?? []).map((p) => (
              <Badge key={p.name} variant="cyan">{p.backend}</Badge>
            ))}
            <Badge variant="neutral">{model.spec.resources.vram}</Badge>
          </div>
          {model.spec.huggingFace?.repo && (
            <p
              className="text-muted text-xs font-mono truncate"
              title={model.spec.huggingFace.repo}
            >
              {model.spec.huggingFace.repo}
            </p>
          )}
          <Labels labels={model.metadata.labels} />
        </div>
      </Card>
    </button>
  );
}

function EnvironmentCard({
  env,
  dimmed,
}: {
  env: InferenceEnvironment;
  dimmed: boolean;
}) {
  const status = deriveStatus(env.status?.conditions);
  const region = envRegion(env);
  const gpuPools = env.status?.capacity?.gpuPools ?? [];

  return (
    <div className={`transition ${dimmed ? "opacity-30" : ""}`}>
      <Card>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <StatusDot status={status} />
            <span className="text-text font-medium text-sm">
              {env.metadata.name}
            </span>
            <Badge variant="neutral">{env.spec.backend}</Badge>
            {region && (
              <span className="text-xs text-muted">{region}</span>
            )}
          </div>
          {gpuPools.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {gpuPools.map((pool, i) => (
                <span key={i} className="text-xs text-muted">
                  {pool.count}x {pool.acceleratorType} ({pool.memory}/GPU)
                </span>
              ))}
            </div>
          )}
          <Labels labels={env.metadata.labels} />
        </div>
      </Card>
    </div>
  );
}
