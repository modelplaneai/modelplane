import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useEnvironments } from "../../hooks/useEnvironments";
import { useEvents } from "../../hooks/useEvents";
import { useApi } from "../../api/context";
import { deriveStatus } from "../../lib/status";
import { relativeAge } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { ConditionList } from "../../components/ConditionList";
import { EventTimeline } from "../../components/EventTimeline";
import type { ModelPlacement, KubeList } from "../../api/types";

export function EnvironmentDetail() {
  const { name } = useParams<{ name: string }>();
  const { data: envsData, isLoading } = useEnvironments();
  const api = useApi();
  const { data: allPlacements } = useQuery<KubeList<ModelPlacement>>({
    queryKey: ["all-placements"],
    queryFn: () => api.listAllModelPlacements(),
  });

  const env = (envsData?.items ?? []).find((e) => e.metadata.name === name);
  const { data: eventsData } = useEvents("default", "InferenceEnvironment", name ?? "", env?.metadata.uid);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading environment…</span>
      </div>
    );
  }

  if (!env) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">Environment not found</span>
      </div>
    );
  }

  const status = deriveStatus(env.status?.conditions);
  const conditions = env.status?.conditions ?? [];
  const age = relativeAge(env.metadata.creationTimestamp);
  const region = env.spec.kserve?.cluster?.gke?.region;
  const gpuPools = env.status?.capacity?.gpuPools ?? [];
  const events = eventsData?.items ?? [];

  // Placements targeting this environment.
  const placements = (allPlacements?.items ?? []).filter(
    (p) => p.spec.inferenceEnvironmentRef.name === name,
  );

  const usedGpus = placements.reduce(
    (sum, p) => sum + (p.status?.resources?.gpu?.count ?? 0),
    0,
  );
  const totalGpus = gpuPools.reduce((s, p) => s + p.count, 0);

  // Infrastructure details from the spec.
  const kserve = env.spec.kserve;
  const gke = kserve?.cluster?.gke;
  const clusterSource = kserve?.cluster?.source;
  const nodePools = gke?.nodePools ?? [];

  return (
    <div className="space-y-8">
      {/* Back link */}
      <Link to="/admin/environments" className="text-muted hover:text-purple-hi transition text-sm">
        &larr; Infrastructure
      </Link>

      {/* Header */}
      <div>
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-2xl font-semibold text-text">{env.metadata.name}</h1>
          <StatusDot status={status} />
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
          <Badge variant="neutral">{env.spec.backend}</Badge>
          {region && <span>{region}</span>}
          <span>&middot;</span>
          <span>{age}</span>
        </div>
      </div>

      {/* Conditions */}
      {conditions.length > 0 && (
        <div>
          <SectionLabel>CONDITIONS</SectionLabel>
          <Card>
            <ConditionList conditions={conditions} />
          </Card>
        </div>
      )}

      {/* Capacity + Placements — the utilization view */}
      <div>
        <SectionLabel>CAPACITY</SectionLabel>
        <Card>
          <div className="space-y-3">
            {/* GPU pools */}
            {gpuPools.length > 0 ? (
              <div className="grid grid-cols-3 gap-3">
                {gpuPools.map((pool, i) => (
                  <div
                    key={i}
                    className="bg-bg border border-border rounded-lg px-3 py-2"
                  >
                    <p className="text-sm font-medium text-text">
                      {pool.acceleratorType}
                    </p>
                    <p className="text-xs text-muted">
                      {pool.memory} VRAM/GPU &middot; {pool.count} available
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted">No GPU pools reported</p>
            )}

            {/* Utilization summary */}
            {totalGpus > 0 && (
              <p className="text-sm text-muted-hi">
                {usedGpus} of {totalGpus} GPUs in use
                ({placements.length} placement{placements.length !== 1 ? "s" : ""})
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* Placements on this environment */}
      {placements.length > 0 && (
        <div>
          <SectionLabel>PLACEMENTS</SectionLabel>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {placements.map((p) => {
              const pStatus = deriveStatus(p.status?.conditions);
              const gpuCount = p.status?.resources?.gpu?.count;
              const deploymentName = p.metadata.labels?.["modelplane.ai/deployment"];
              const pConditions = (p.status?.conditions ?? []).filter(
                (c) => c.type !== "Ready" && c.type !== "Synced" && c.type !== "Responsive",
              );
              return (
                <Link
                  key={p.metadata.name}
                  to={`/placements/${p.metadata.namespace}/${p.metadata.name}`}
                  className="block hover:ring-1 hover:ring-border-hi rounded-xl transition"
                >
                  <Card>
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <StatusDot status={pStatus} />
                        <span className="text-text font-medium text-sm">{p.spec.modelRef.name}</span>
                      </div>
                      <div className="flex flex-wrap gap-2 text-xs text-muted">
                        {deploymentName && (
                          <span>
                            deploy: <span className="text-muted-hi font-mono">{deploymentName}</span>
                          </span>
                        )}
                        {gpuCount !== undefined && (
                          <Badge variant="neutral">
                            {gpuCount} GPU{gpuCount !== 1 ? "s" : ""}
                          </Badge>
                        )}
                      </div>
                      {pConditions.length > 0 && (
                        <div className="pt-1">
                          <ConditionList conditions={pConditions} />
                        </div>
                      )}
                    </div>
                  </Card>
                </Link>
              );
            })}
          </div>
        </div>
      )}

      {/* Backend + Cluster */}
      <div>
        <SectionLabel>INFRASTRUCTURE</SectionLabel>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Backend */}
          <Card>
            <div className="space-y-2">
              <p className="font-mono text-[11px] uppercase tracking-wider text-muted">Backend</p>
              <p className="text-text font-medium">{env.spec.backend}</p>
              {kserve?.version && (
                <p className="text-sm text-muted">Version: <span className="text-muted-hi">{kserve.version}</span></p>
              )}
              {env.status?.providerConfigRef?.name && (
                <p className="text-sm text-muted">
                  ProviderConfig: <span className="text-muted-hi font-mono">{env.status.providerConfigRef.name}</span>
                </p>
              )}
              {env.status?.gateway?.address && (
                <p className="text-sm text-muted">
                  Gateway: <span className="text-muted-hi font-mono">{env.status.gateway.address}</span>
                </p>
              )}
            </div>
          </Card>

          {/* Cluster */}
          {clusterSource && (
            <Card>
              <div className="space-y-2">
                <p className="font-mono text-[11px] uppercase tracking-wider text-muted">Cluster</p>
                <p className="text-text font-medium">{clusterSource}</p>
                {gke && (
                  <>
                    <p className="text-sm text-muted">
                      Project: <span className="text-muted-hi font-mono">{gke.project}</span>
                    </p>
                    <p className="text-sm text-muted">
                      Region: <span className="text-muted-hi font-mono">{gke.region}</span>
                    </p>
                  </>
                )}
              </div>
            </Card>
          )}
        </div>

        {/* Node pools */}
        {nodePools.length > 0 && (
          <div className="grid grid-cols-2 gap-3 mt-4">
            {nodePools.map((np) => (
              <Card key={np.name}>
                <p className="text-text font-medium text-sm">{np.name}</p>
                <p className="text-xs text-muted">
                  {np.machineType} &middot; {np.nodeCount ?? 1} node{(np.nodeCount ?? 1) !== 1 ? "s" : ""}
                  {np.gpu && ` · ${np.gpu.acceleratorCount ?? 1}x ${np.gpu.acceleratorType}`}
                </p>
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* Events */}
      {events.length > 0 && (
        <div>
          <SectionLabel>EVENTS</SectionLabel>
          <Card>
            <EventTimeline events={events} />
          </Card>
        </div>
      )}
    </div>
  );
}
