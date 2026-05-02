import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useEnvironments } from "../../hooks/useEnvironments";
import { useApi } from "../../api/context";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { ConditionList } from "../../components/ConditionList";
import { deriveStatus, statusText } from "../../lib/status";
import { envRegion, envClusterSource } from "../../lib/format";
import type { InferenceGateway, InferenceEnvironment, ModelPlacement, KubeList } from "../../api/types";

export function InfrastructurePage() {
  const api = useApi();
  const { data: envsData, isLoading: envsLoading } = useEnvironments();
  const { data: gwData } = useQuery<KubeList<InferenceGateway>>({
    queryKey: ["inferencegateways"],
    queryFn: () => api.listInferenceGateways(),
  });
  const { data: allPlacements } = useQuery<KubeList<ModelPlacement>>({
    queryKey: ["all-placements"],
    queryFn: () => api.listAllModelPlacements(),
  });

  const environments = envsData?.items ?? [];
  const gateways = gwData?.items ?? [];
  const gateway = gateways[0]; // Singleton in practice.
  const placements = allPlacements?.items ?? [];

  // Compute summary stats.
  const totalGpus = environments.reduce((sum, env) => {
    const pools = env.status?.capacity?.gpuPools ?? [];
    return sum + pools.reduce((s, p) => s + p.count, 0);
  }, 0);

  const usedGpus = placements.reduce(
    (sum, p) => sum + (p.status?.resources?.gpu?.count ?? 0),
    0,
  );

  const healthyEnvs = environments.filter(
    (e) => deriveStatus(e.status?.conditions) === "ready",
  ).length;

  const readyPlacements = placements.filter(
    (p) => deriveStatus(p.status?.conditions) === "ready",
  ).length;

  const unhealthyEnvs = environments.filter(
    (e) => deriveStatus(e.status?.conditions) === "error",
  );

  if (envsLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-muted">
        Loading infrastructure…
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Summary — gateway and environments side by side */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Gateway card */}
        <Card>
          <div className="space-y-3">
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted">Gateway</p>
            {gateway ? (
              <>
                <div className="flex items-center gap-2">
                  <StatusDot status={deriveStatus(gateway.status?.conditions)} />
                  <span className="text-text font-medium">{gateway.metadata.name}</span>
                  <Badge variant="neutral">{gateway.spec.backend}</Badge>
                </div>
                <p className="font-mono text-lg text-text">
                  {gateway.status?.address ?? "No address"}
                </p>
                <ConditionList
                  conditions={(gateway.status?.conditions ?? []).filter(
                    (c) => c.type !== "Synced" && c.type !== "Responsive",
                  )}
                />
              </>
            ) : (
              <div className="flex items-center gap-2">
                <StatusDot status="error" />
                <span className="text-sm text-muted">No InferenceGateway found</span>
              </div>
            )}
          </div>
        </Card>

        {/* Environments summary card */}
        <Card>
          <div className="space-y-3">
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted">Environments</p>
            <div className="space-y-2">
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold text-text">{environments.length}</span>
                <span className="text-sm text-muted">
                  environment{environments.length !== 1 ? "s" : ""}
                </span>
                <span className="text-sm text-muted-hi">({healthyEnvs} healthy)</span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold text-text">{totalGpus}</span>
                <span className="text-sm text-muted">GPUs</span>
                <span className="text-sm text-muted-hi">({usedGpus} in use)</span>
              </div>
              <div className="flex items-baseline gap-2">
                <span className="text-2xl font-semibold text-text">{placements.length}</span>
                <span className="text-sm text-muted">
                  placement{placements.length !== 1 ? "s" : ""}
                </span>
                <span className="text-sm text-muted-hi">({readyPlacements} ready)</span>
              </div>
            </div>
            {unhealthyEnvs.length > 0 && (
              <p className="text-sm text-red">
                Unhealthy: {unhealthyEnvs.map((e) => e.metadata.name).join(", ")}
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* Environments table */}
      <div>
        <SectionLabel>ENVIRONMENTS</SectionLabel>
        <table className="w-full">
          <thead>
            <tr className="font-mono text-[11px] uppercase tracking-wider text-muted">
              <th className="text-left px-4 py-2 font-normal">Name</th>
              <th className="text-left px-4 py-2 font-normal">Cluster</th>
              <th className="text-left px-4 py-2 font-normal">Region</th>
              <th className="text-left px-4 py-2 font-normal">GPUs</th>
              <th className="text-left px-4 py-2 font-normal">Placements</th>
              <th className="text-left px-4 py-2 font-normal">Status</th>
            </tr>
          </thead>
          <tbody>
            {environments.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-sm text-muted">
                  No inference environments found
                </td>
              </tr>
            )}
            {environments.map((env) => (
              <EnvironmentRow
                key={env.metadata.name}
                env={env}
                placements={placements.filter(
                  (p) => p.spec.inferenceEnvironmentRef.name === env.metadata.name,
                )}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EnvironmentRow({
  env,
  placements,
}: {
  env: InferenceEnvironment;
  placements: ModelPlacement[];
}) {
  const status = deriveStatus(env.status?.conditions);
  const region = envRegion(env) ?? "—";
  const gpuPools = env.status?.capacity?.gpuPools ?? [];
  const totalGpus = gpuPools.reduce((s, p) => s + p.count, 0);
  const gpuSummary = gpuPools.length > 0
    ? gpuPools.map((p) => `${p.count}x ${p.acceleratorType}`).join(", ")
    : "—";

  return (
    <tr className="border-b border-border hover:bg-bg-mid/50 transition">
      <td className="px-4 py-3 text-sm">
        <Link
          to={`/admin/environments/${env.metadata.name}`}
          className="flex items-center gap-2 text-text hover:text-purple-hi transition"
        >
          <StatusDot status={status} />
          <span className="font-medium">{env.metadata.name}</span>
        </Link>
      </td>
      <td className="px-4 py-3 text-sm text-muted-hi">{envClusterSource(env) ?? "—"}</td>
      <td className="px-4 py-3 text-sm text-muted-hi">{region}</td>
      <td className="px-4 py-3 text-sm text-muted-hi" title={gpuSummary}>
        {totalGpus > 0 ? totalGpus : "—"}
      </td>
      <td className="px-4 py-3 text-sm text-muted-hi">{placements.length}</td>
      <td className="px-4 py-3 text-sm text-muted-hi">{statusText(env.status?.conditions)}</td>
    </tr>
  );
}
