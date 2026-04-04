import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "../../components/Modal";
import { Button } from "../../components/Button";
import { Badge } from "../../components/Badge";
import { useModels } from "../../hooks/useModels";
import { useApi } from "../../api/context";
import { modelDisplayName, isValidKubernetesName, toKubernetesName } from "../../lib/format";
import { DEFAULT_NAMESPACE } from "../../lib/config";

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
  const [deploymentName, setDeploymentName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const [environments, setEnvironments] = useState("1");
  const [namespace, setNamespace] = useState(DEFAULT_NAMESPACE);
  const [deploying, setDeploying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sync preselectedModel and reset name when the modal opens.
  useEffect(() => {
    if (open) {
      setNameEdited(false);
      if (preselectedModel) {
        setSelectedModel(preselectedModel);
      }
    }
  }, [open, preselectedModel]);

  // Default to first model if nothing is selected.
  useEffect(() => {
    if (open && !selectedModel && models.length > 0) {
      setSelectedModel(models[0].metadata.name);
    }
  }, [open, selectedModel, models]);

  // Derive a default deployment name from the selected model unless the
  // user has manually edited the name field.
  const selected = models.find((m) => m.metadata.name === selectedModel);
  useEffect(() => {
    if (selected && !nameEdited) {
      setDeploymentName(toKubernetesName(`${selected.metadata.name}-deployment`));
    }
  }, [selected, nameEdited]);

  const nameInvalid = deploymentName !== "" && !isValidKubernetesName(deploymentName);

  async function handleDeploy() {
    if (!selected) return;
    setDeploying(true);
    setError(null);

    const name = deploymentName;

    try {
      await api.createModelDeployment(namespace, {
        apiVersion: "modelplane.ai/v1alpha1",
        kind: "ModelDeployment",
        metadata: { name, namespace },
        spec: {
          modelRef: { kind: "ClusterModel", name: selected.metadata.name },
          environments: Math.max(1, parseInt(environments) || 1),
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
              {(selected.spec.serving ?? []).map((p) => (
                <Badge key={p.name} variant="cyan">{p.backend}</Badge>
              ))}
              <Badge variant="neutral">{selected.spec.resources.vram}</Badge>
            </div>
            {selected.spec.huggingFace?.repo && (
              <p className="text-xs font-mono text-muted">{selected.spec.huggingFace.repo}</p>
            )}
          </div>
        )}

        {/* Deployment name */}
        <div>
          <label className="block text-xs font-mono uppercase tracking-wider text-muted mb-1.5">
            Name
          </label>
          <input
            type="text"
            required
            value={deploymentName}
            onChange={(e) => { setDeploymentName(e.target.value); setNameEdited(true); }}
            className="w-full bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-border-hi"
          />
          {nameInvalid && (
            <p className="text-xs text-red mt-1">
              Must be lowercase alphanumeric or hyphens, and start/end with an alphanumeric character.
            </p>
          )}
        </div>

        {/* Environments */}
        <div>
          <label className="block text-xs font-mono uppercase tracking-wider text-muted mb-1.5">
            Environments
          </label>
          <input
            type="number"
            min={1}
            value={environments}
            onChange={(e) => setEnvironments(e.target.value)}
            onBlur={() => setEnvironments(String(Math.max(1, parseInt(environments) || 1)))}
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
