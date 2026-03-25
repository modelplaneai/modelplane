import { useState } from "react";
import { Link } from "react-router-dom";
import { useDeployments } from "../../hooks/useDeployments";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Button } from "../../components/Button";
import { DeployModal } from "./DeployModal";
import { deriveStatus, statusText } from "../../lib/status";
import { DEFAULT_NAMESPACE } from "../../lib/config";

export function DeploymentsPage() {
  const { data, isLoading, error } = useDeployments(DEFAULT_NAMESPACE);
  const [deployOpen, setDeployOpen] = useState(false);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading deployments…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">
          Failed to load deployments: {error instanceof Error ? error.message : "Unknown error"}
        </span>
      </div>
    );
  }

  const deployments = data?.items ?? [];

  return (
    <div>
      <div className="flex items-start justify-between mb-6">
        <div>
          <SectionLabel>DEPLOYMENTS</SectionLabel>
          <p className="text-muted text-sm -mt-2">
            Namespace: <span className="font-mono text-muted-hi">{DEFAULT_NAMESPACE}</span>
          </p>
        </div>
        <Button variant="primary" onClick={() => setDeployOpen(true)}>
          Deploy Model
        </Button>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="font-mono text-[11px] uppercase tracking-wider text-muted bg-bg-mid">
              <th className="text-left px-4 py-2.5">Name</th>
              <th className="text-left px-4 py-2.5">Model</th>
              <th className="text-left px-4 py-2.5">Envs</th>
              <th className="text-left px-4 py-2.5">Endpoint</th>
              <th className="text-left px-4 py-2.5">Status</th>
            </tr>
          </thead>
          <tbody>
            {deployments.map((dep) => {
              const ns = dep.metadata.namespace ?? DEFAULT_NAMESPACE;
              const name = dep.metadata.name;
              const status = deriveStatus(dep.status?.conditions);
              const model = dep.status?.model?.name ?? dep.spec.modelRef.name;
              const endpoint = dep.status?.endpoint?.url;
              const placements = dep.status?.placements;
              const placementsLabel = placements ? `${placements.ready}/${placements.total}` : "—";

              return (
                <tr
                  key={name}
                  className="border-b border-border hover:bg-bg-mid transition"
                >
                  <td className="px-4 py-3">
                    <Link
                      to={`/deployments/${ns}/${name}`}
                      className="flex items-center gap-2 text-text hover:text-purple-hi transition"
                    >
                      <StatusDot status={status} />
                      <span className="font-medium">{name}</span>
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-hi font-mono">{model}</td>
                  <td className="px-4 py-3 text-sm text-muted-hi">{placementsLabel}</td>
                  <td className="px-4 py-3 text-sm text-muted font-mono max-w-xs truncate">
                    {endpoint ? (
                      <span title={endpoint}>{endpoint}</span>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-hi">{statusText(dep.status?.conditions)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {deployments.length === 0 && (
        <p className="text-muted text-sm text-center py-12">
          No deployments found. Deploy a model to get started.
        </p>
      )}

      <DeployModal open={deployOpen} onClose={() => setDeployOpen(false)} />
    </div>
  );
}
