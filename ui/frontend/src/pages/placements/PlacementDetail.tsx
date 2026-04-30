import { Link, useParams } from "react-router-dom";
import { usePlacements } from "../../hooks/usePlacements";
import { useEnvironments } from "../../hooks/useEnvironments";
import { useEvents } from "../../hooks/useEvents";
import { deriveStatus } from "../../lib/status";
import { relativeAge, envRegion, envClusterSource, poolGpuCount } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { Labels } from "../../components/Labels";
import { ConditionList } from "../../components/ConditionList";
import { EventTimeline } from "../../components/EventTimeline";

export function PlacementDetail() {
  const { ns, name } = useParams<{ ns: string; name: string }>();
  const { data: placementsData, isLoading } = usePlacements(ns ?? "");
  const { data: envsData } = useEnvironments();

  const placement = (placementsData?.items ?? []).find(
    (p) => p.metadata.name === name,
  );

  const placementUid = placement?.metadata.uid;
  const { data: eventsData } = useEvents(ns ?? "", "ModelPlacement", name ?? "", placementUid);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading placement…</span>
      </div>
    );
  }

  if (!placement) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">Placement not found</span>
      </div>
    );
  }

  const status = deriveStatus(placement.status?.conditions);
  const conditions = placement.status?.conditions ?? [];
  const age = relativeAge(placement.metadata.creationTimestamp);
  const envName = placement.spec.inferenceEnvironmentRef.name;
  const modelName = placement.spec.modelRef.name;
  const deploymentName = placement.metadata.labels?.["modelplane.ai/deployment"];
  const gpuCount = placement.status?.resources?.gpu?.count;
  const endpoint = placement.status?.endpoint?.url;
  const events = eventsData?.items ?? [];

  // Find the referenced InferenceEnvironment for extra context.
  const env = (envsData?.items ?? []).find((e) => e.metadata.name === envName);
  const region = env ? envRegion(env) : undefined;
  const clusterSource = env ? envClusterSource(env) : undefined;
  const gpuPools = env?.status?.capacity?.gpuPools ?? [];

  return (
    <div className="space-y-8">
      {/* Back link */}
      {deploymentName ? (
        <Link
          to={`/deployments/${ns}/${deploymentName}`}
          className="text-muted hover:text-purple-hi transition text-sm"
        >
          &larr; {deploymentName}
        </Link>
      ) : (
        <Link to="/deployments" className="text-muted hover:text-purple-hi transition text-sm">
          &larr; Deployments
        </Link>
      )}

      {/* Header */}
      <div>
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-2xl font-semibold text-text">{placement.metadata.name}</h1>
          <StatusDot status={status} />
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
          <span>
            model: <span className="font-mono text-muted-hi">{modelName}</span>
          </span>
          <span>&middot;</span>
          <span>
            ns: <span className="font-mono text-muted-hi">{ns}</span>
          </span>
          <span>&middot;</span>
          <span>{age}</span>
        </div>
        <Labels labels={placement.metadata.labels} className="mt-2" />
      </div>

      {/* Environment — the defining context for this placement */}
      <div>
        <SectionLabel>ENVIRONMENT</SectionLabel>
        <Card>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-text font-medium">{envName}</span>
              {clusterSource && <Badge variant="neutral">{clusterSource}</Badge>}
              {region && <Badge variant="cyan">{region}</Badge>}
            </div>
            {gpuPools.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {gpuPools.map((pool, i) => (
                  <span key={i} className="text-xs text-muted">
                    {pool.acceleratorType} &middot; {pool.memory}/GPU &middot; {poolGpuCount(pool)} available
                  </span>
                ))}
              </div>
            )}
            {gpuCount !== undefined && (
              <p className="text-sm text-muted-hi">
                This placement uses {gpuCount} GPU{gpuCount !== 1 ? "s" : ""}
              </p>
            )}
          </div>
        </Card>
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

      {/* Endpoint */}
      {endpoint && (
        <div>
          <SectionLabel>ENDPOINT</SectionLabel>
          <Card>
            <code className="font-mono text-sm text-text">{endpoint}</code>
          </Card>
        </div>
      )}

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
