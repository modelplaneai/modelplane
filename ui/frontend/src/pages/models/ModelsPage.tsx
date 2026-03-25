import { useState } from "react";
import { useModels } from "../../hooks/useModels";
import { SectionLabel } from "../../components/SectionLabel";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DeployModal } from "../deployments/DeployModal";
import { modelDisplayName } from "../../lib/format";

export function ModelsPage() {
  const { data, isLoading, error } = useModels();
  const [deployOpen, setDeployOpen] = useState(false);
  const [preselected, setPreselected] = useState<string | undefined>();

  function openDeploy(modelName?: string) {
    setPreselected(modelName);
    setDeployOpen(true);
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading models…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">
          Failed to load models: {error instanceof Error ? error.message : "Unknown error"}
        </span>
      </div>
    );
  }

  const models = data?.items ?? [];

  return (
    <div>
      <div className="mb-6">
        <SectionLabel>MODEL CATALOG</SectionLabel>
        <p className="text-muted text-sm -mt-2">Browse models available for deployment</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {models.map((model) => (
          <Card key={model.metadata.name}>
            <div className="flex flex-col gap-3">
              <h3 className="text-text font-medium">{modelDisplayName(model.spec.model.name)}</h3>

              <div className="flex flex-wrap gap-2">
                <Badge variant="cyan">{model.spec.engine}</Badge>
                <Badge variant="neutral">{model.spec.resources.vram}</Badge>
              </div>

              {model.spec.huggingFace?.repo && (
                <p className="text-muted text-sm font-mono truncate" title={model.spec.huggingFace.repo}>
                  {model.spec.huggingFace.repo}
                </p>
              )}

              <div className="pt-1">
                <Button variant="primary" onClick={() => openDeploy(model.metadata.name)}>
                  Deploy
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {models.length === 0 && (
        <p className="text-muted text-sm text-center py-12">No models found in the cluster.</p>
      )}

      <DeployModal
        open={deployOpen}
        onClose={() => setDeployOpen(false)}
        preselectedModel={preselected}
      />
    </div>
  );
}
