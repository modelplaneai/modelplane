import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "../../components/Modal";
import { Button } from "../../components/Button";
import { Badge } from "../../components/Badge";
import { useModels } from "../../hooks/useModels";
import { useApi } from "../../api/context";
import { modelDisplayName, isValidKubernetesName } from "../../lib/format";
import { DEFAULT_NAMESPACE } from "../../lib/config";
import type { ClusterModel } from "../../api/types";

interface DeployModalProps {
  open: boolean;
  onClose: () => void;
  preselectedModel?: string;
}

export function DeployModal({ open, onClose, preselectedModel }: DeployModalProps) {
  const navigate = useNavigate();
  const api = useApi();
  const { data: modelsData } = useModels();
  const models = useMemo(() => modelsData?.items ?? [], [modelsData]);

  const [selectedModel, setSelectedModel] = useState(preselectedModel ?? "");
  const [environments, setEnvironments] = useState(1);
  const [namespace, setNamespace] = useState(DEFAULT_NAMESPACE);
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sync preselectedModel when it changes or modal opens.
  useEffect(() => {
    if (open && preselectedModel) {
      setSelectedModel(preselectedModel);
    }
  }, [open, preselectedModel]);

  // Default to first model if nothing is selected.
  useEffect(() => {
    if (open && !selectedModel && models.length > 0) {
      setSelectedModel(models[0].metadata.name);
    }
  }, [open, selectedModel, models]);

  const selected = models.find((m) => m.metadata.name === selectedModel);
  const deploymentName = selected ? `${selected.metadata.name}-deployment` : "";
  const nameInvalid = deploymentName !== "" && !isValidKubernetesName(deploymentName);

  async function handleDeploy() {
    if (!selected) return;
    setDeploying(true);
    setError(null);

    const name = `${selected.metadata.name}-deployment`;

    try {
      await api.createModelDeployment(namespace, {
        apiVersion: "modelplane.ai/v1alpha1",
        kind: "ModelDeployment",
        metadata: { name, namespace },
        spec: {
          modelRef: { kind: "ClusterModel", name: selected.metadata.name },
          environments,
        },
      });
      onClose();
      navigate(`/deployments/${namespace}/${name}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Deployment failed");
    } finally {
      setDeploying(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Deploy Model">
      <div className="space-y-4">
        {/* Model select */}
        <div>
          <label className="block text-xs font-mono uppercase tracking-wider text-muted mb-1.5">
            Model
          </label>
          <select
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            className="w-full bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
          >
            {models.map((m) => (
              <option key={m.metadata.name} value={m.metadata.name}>
                {m.metadata.name} — {m.spec.model.name}
              </option>
            ))}
          </select>
        </div>

        {/* Model details */}
        {selected && (
          <div className="bg-bg-mid border border-border rounded-lg p-3 space-y-1">
            <p className="text-sm text-text">{modelDisplayName(selected.spec.model.name)}</p>
            <div className="flex gap-2">
              <Badge variant="cyan">{selected.spec.engine}</Badge>
              <Badge variant="neutral">{selected.spec.resources.vram}</Badge>
            </div>
            {selected.spec.huggingFace?.repo && (
              <p className="text-xs font-mono text-muted">{selected.spec.huggingFace.repo}</p>
            )}
          </div>
        )}

        {/* Deployment name preview + validation */}
        {deploymentName && (
          <div>
            <p className="text-xs font-mono text-muted">
              Name: <span className="text-muted-hi">{deploymentName}</span>
            </p>
            {nameInvalid && (
              <p className="text-xs text-red mt-1">
                Invalid Kubernetes name. Must be lowercase alphanumeric or hyphens, and start/end with an alphanumeric character.
              </p>
            )}
          </div>
        )}

        {/* Environments */}
        <div>
          <label className="block text-xs font-mono uppercase tracking-wider text-muted mb-1.5">
            Environments
          </label>
          <input
            type="number"
            min={1}
            value={environments}
            onChange={(e) => setEnvironments(Math.max(1, parseInt(e.target.value) || 1))}
            className="w-full bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
          />
        </div>

        {/* Namespace */}
        <div>
          <label className="block text-xs font-mono uppercase tracking-wider text-muted mb-1.5">
            Namespace
          </label>
          <input
            type="text"
            value={namespace}
            onChange={(e) => setNamespace(e.target.value)}
            className="w-full bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
          />
        </div>

        {error && <p className="text-sm text-red">{error}</p>}

        {/* Actions */}
        <div className="flex justify-end gap-3 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={deploying}>
            Cancel
          </Button>
          <Button onClick={handleDeploy} disabled={deploying || !selected || nameInvalid}>
            {deploying ? "Deploying…" : "Deploy"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
