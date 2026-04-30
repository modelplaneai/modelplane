import { Fragment, useState } from "react";
import { Link } from "react-router-dom";
import { useEnvironments } from "../../hooks/useEnvironments";
import { useEvents } from "../../hooks/useEvents";
import { useApi } from "../../api/context";
import { useQuery } from "@tanstack/react-query";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import { ConditionList } from "../../components/ConditionList";
import { EventTimeline } from "../../components/EventTimeline";
import { deriveStatus, statusText } from "../../lib/status";
import { envRegion, envClusterSource, poolGpuCount } from "../../lib/format";
import type { InferenceEnvironment, ModelPlacement, KubeList } from "../../api/types";

function EnvironmentDetailRow({ env, placements }: { env: InferenceEnvironment; placements: ModelPlacement[] }) {
  const conditions = env.status?.conditions ?? [];
  const gpuPools = env.status?.capacity?.gpuPools ?? [];

  // Cluster-scoped resource events land in the default namespace.
  const { data: eventsData } = useEvents("default", "InferenceEnvironment", env.metadata.name, env.metadata.uid);
  const events = eventsData?.items ?? [];

  return (
    <tr>
      <td colSpan={5} className="px-4 py-4 border-b border-border bg-bg-mid">
        <div className="grid grid-cols-2 gap-6">
          {/* Left column: conditions */}
          <div>
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-2">
              Conditions
            </p>
            <ConditionList conditions={conditions} />
          </div>

          {/* Right column: metadata */}
          <div className="space-y-3">
            <div>
              <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
                Cluster Source
              </p>
              <p className="text-sm text-text">
                {envClusterSource(env) ?? "—"}
              </p>
            </div>
            <div>
              <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
                ProviderConfig
              </p>
              <p className="text-sm text-text">
                {env.status?.providerConfigRef?.name ?? "—"}
              </p>
            </div>
            <div>
              <p className="font-mono text-[11px] uppercase tracking-wider text-muted">
                Internal Namespace
              </p>
              <p className="text-sm text-text">
                {env.status?.namespace ?? "—"}
              </p>
            </div>
          </div>
        </div>

        {/* GPU Pools */}
        {gpuPools.length > 0 && (
          <div className="mt-4">
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-2">
              GPU Pools
            </p>
            <div className="grid grid-cols-3 gap-3">
              {gpuPools.map((pool, i) => (
                <div
                  key={i}
                  className="bg-bg-card border border-border rounded-lg px-3 py-2"
                >
                  <p className="text-sm font-medium text-text">
                    {pool.acceleratorType}
                  </p>
                  <p className="text-xs text-muted">
                    {pool.memory} VRAM/GPU &middot; {poolGpuCount(pool)} available
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Model Placements */}
        {placements.length > 0 && (
          <div className="mt-4">
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-2">
              Model Placements
            </p>
            <div className="grid grid-cols-2 gap-3">
              {placements.map((p) => {
                const pStatus = deriveStatus(p.status?.conditions);
                const gpuCount = p.status?.resources?.gpu?.count;
                const pConditions = (p.status?.conditions ?? []).filter(
                  (c) => c.type !== "Ready" && c.type !== "Synced" && c.type !== "Responsive",
                );
                const deploymentName = p.metadata.labels?.["modelplane.ai/deployment"];
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

        {/* Events */}
        {events.length > 0 && (
          <div className="mt-4">
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-2">
              Events
            </p>
            <EventTimeline events={events} />
          </div>
        )}
      </td>
    </tr>
  );
}

export function EnvironmentsPage() {
  const { data, isLoading, error } = useEnvironments();
  const api = useApi();
  const { data: allPlacements } = useQuery<KubeList<ModelPlacement>>({
    queryKey: ["all-placements"],
    queryFn: () => api.listAllModelPlacements(),
  });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) {
        next.delete(name);
      } else {
        next.add(name);
      }
      return next;
    });
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-muted">
        Loading environments…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-20 text-red">
        Failed to load environments:{" "}
        {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const environments = data?.items ?? [];

  return (
    <div>
      <SectionLabel>Inference Environments</SectionLabel>

      <table className="w-full">
        <thead>
          <tr className="font-mono text-[11px] uppercase tracking-wider text-muted">
            <th className="text-left px-4 py-2 font-normal">Name</th>
            <th className="text-left px-4 py-2 font-normal">Cluster</th>
            <th className="text-left px-4 py-2 font-normal">Region</th>
            <th className="text-left px-4 py-2 font-normal">Gateway</th>
            <th className="text-left px-4 py-2 font-normal">Status</th>
          </tr>
        </thead>
        <tbody>
          {environments.length === 0 && (
            <tr>
              <td
                colSpan={5}
                className="px-4 py-8 text-center text-sm text-muted"
              >
                No inference environments found
              </td>
            </tr>
          )}
          {environments.map((env) => {
            const name = env.metadata.name;
            const isExpanded = expanded.has(name);
            const status = deriveStatus(env.status?.conditions);

            return (
              <Fragment key={name}>
                <tr
                  className="border-b border-border hover:bg-bg-mid/50 cursor-pointer transition"
                  onClick={() => toggle(name)}
                >
                  <td className="px-4 py-3 text-sm">
                    <span className="flex items-center gap-2">
                      <StatusDot status={status} />
                      <span className="text-text">{name}</span>
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-hi">
                    {envClusterSource(env) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-hi">
                    {envRegion(env) ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-sm font-mono text-muted-hi">
                    {env.status?.gateway?.address ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-hi">
                    {statusText(env.status?.conditions)}
                  </td>
                </tr>
                {isExpanded && (
                  <EnvironmentDetailRow
                    env={env}
                    placements={(allPlacements?.items ?? []).filter(
                      (p) => p.spec.inferenceEnvironmentRef.name === name,
                    )}
                  />
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
