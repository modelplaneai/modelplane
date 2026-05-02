import { useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useDeployment } from "../../hooks/useDeployments";
import { usePlacements } from "../../hooks/usePlacements";
import { useEvents } from "../../hooks/useEvents";
import { useApi } from "../../api/context";
import { deriveStatus } from "../../lib/status";
import { relativeAge } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { CurlSnippet } from "../../components/CurlSnippet";
import { ChatWidget } from "../../components/ChatWidget";
import { Labels } from "../../components/Labels";
import { ConditionList } from "../../components/ConditionList";
import { EventTimeline } from "../../components/EventTimeline";
import type { ModelPlacement, Scaling } from "../../api/types";

function scalingSummary(scaling?: Scaling): string | null {
  if (!scaling) return null;
  if (scaling.signal === "Fixed" && scaling.fixed) {
    return `${scaling.fixed.replicas} replica${scaling.fixed.replicas !== 1 ? "s" : ""}`;
  }
  if (scaling.signal === "Concurrency" && scaling.concurrency) {
    const c = scaling.concurrency;
    const min = c.minReplicas ?? 1;
    return `${min}–${c.maxReplicas} replicas, target ${c.target} rq/replica`;
  }
  return null;
}

export function DeploymentDetail() {
  const { ns, name } = useParams<{ ns: string; name: string }>();
  const navigate = useNavigate();
  const api = useApi();
  const { data: deployment, isLoading, error } = useDeployment(ns ?? "", name ?? "");
  const { data: placementsData } = usePlacements(ns ?? "");
  const deploymentUid = deployment?.metadata.uid;
  const { data: deploymentEvents } = useEvents(ns ?? "", "ModelDeployment", name ?? "", deploymentUid);
  const [showCurl, setShowCurl] = useState(false);
  const [endpointCopied, setEndpointCopied] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading deployment…</span>
      </div>
    );
  }

  if (error || !deployment) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">
          {error instanceof Error ? error.message : "Deployment not found"}
        </span>
      </div>
    );
  }

  const status = deriveStatus(deployment.status?.conditions);
  const isReady = status === "ready";
  const modelName = deployment.status?.model?.name ?? deployment.spec.modelRef.name;
  const endpointUrl = deployment.status?.endpoint?.url;
  const age = relativeAge(deployment.metadata.creationTimestamp);
  const conditions = deployment.status?.conditions ?? [];

  // Filter placements belonging to this deployment.
  const placements: ModelPlacement[] = (placementsData?.items ?? []).filter(
    (p) => p.metadata.labels?.["modelplane.ai/deployment"] === name,
  );

  const allEvents = [...(deploymentEvents?.items ?? [])];

  async function copyEndpoint() {
    if (!endpointUrl) return;
    await navigator.clipboard.writeText(endpointUrl);
    setEndpointCopied(true);
    setTimeout(() => setEndpointCopied(false), 2000);
  }

  async function handleDelete() {
    if (!confirm(`Delete deployment ${name}?`)) return;
    setDeleting(true);
    try {
      await api.deleteModelDeployment(ns ?? "", name ?? "");
      navigate("/deployments");
    } catch {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-8">
      {/* Back link */}
      <Link to="/deployments" className="text-muted hover:text-purple-hi transition text-sm">
        &larr; Deployments
      </Link>

      {/* Header */}
      <div className="flex items-start gap-4">
        <div className="flex-1">
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-2xl font-semibold text-text">{deployment.metadata.name}</h1>
            <StatusDot status={status} />
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
            <span className="font-mono text-muted-hi">{modelName}</span>
            <span>&middot;</span>
            <span>
              ns: <span className="font-mono text-muted-hi">{deployment.metadata.namespace}</span>
            </span>
            <span>&middot;</span>
            <span>{age}</span>
          </div>
          {scalingSummary(deployment.spec.scaling) && (
            <div className="flex items-center gap-2 mt-1">
              <Badge variant={deployment.spec.scaling?.signal === "Concurrency" ? "purple" : "neutral"}>
                {deployment.spec.scaling?.signal === "Concurrency" ? "Autoscaling" : "Fixed"}
              </Badge>
              <span className="text-sm text-muted">
                {scalingSummary(deployment.spec.scaling)}
              </span>
            </div>
          )}
          <Labels labels={deployment.metadata.labels} className="mt-2" />
        </div>
        <Button variant="ghost" onClick={handleDelete} disabled={deleting} className="text-red hover:text-red">
          {deleting ? "Deleting…" : "Delete"}
        </Button>
      </div>

      {/* Hero — adapts to lifecycle state */}
      {isReady && endpointUrl ? (
        <div>
          <Card>
            <div className="space-y-4">
              {/* Endpoint */}
              <div className="flex items-center gap-3">
                <code className="font-mono text-lg text-text flex-1 truncate">{endpointUrl}</code>
                <Button variant="ghost" onClick={copyEndpoint} className="shrink-0">
                  {endpointCopied ? "Copied" : "Copy"}
                </Button>
                <Button variant="ghost" onClick={() => setShowCurl(!showCurl)} className="shrink-0">
                  {showCurl ? "Hide cURL" : "cURL"}
                </Button>
              </div>
              {showCurl && <CurlSnippet url={endpointUrl} model={modelName} />}

              {/* Chat inline */}
              <div className="border-t border-border pt-4">
                <ChatWidget namespace={ns ?? ""} deployment={name ?? ""} model={modelName} />
              </div>
            </div>
          </Card>
        </div>
      ) : (
        <Card>
          <ConditionList conditions={conditions} />
        </Card>
      )}

      {/* Placements */}
      <div>
        <SectionLabel>PLACEMENTS</SectionLabel>
        {placements.length === 0 ? (
          <p className="text-muted text-sm">No placements yet.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {placements.map((p) => (
              <PlacementCard key={p.metadata.name} placement={p} />
            ))}
          </div>
        )}
      </div>

      {/* Activity — conditions + events, collapsible in steady state */}
      <div>
        {isReady ? (
          <>
            <button
              onClick={() => setDetailsOpen(!detailsOpen)}
              className="flex items-center gap-2 text-muted hover:text-muted-hi transition text-xs font-mono uppercase tracking-wider mb-4"
            >
              <span className={`transition ${detailsOpen ? "rotate-90" : ""}`}>&#9654;</span>
              Activity
            </button>
            {detailsOpen && (
              <div className="space-y-6">
                {conditions.length > 0 && (
                  <Card>
                    <ConditionList conditions={conditions} />
                  </Card>
                )}
                {allEvents.length > 0 && (
                  <Card>
                    <EventTimeline events={allEvents} />
                  </Card>
                )}
              </div>
            )}
          </>
        ) : (
          <>
            {allEvents.length > 0 && (
              <>
                <SectionLabel>EVENTS</SectionLabel>
                <Card>
                  <EventTimeline events={allEvents} />
                </Card>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function PlacementCard({ placement }: { placement: ModelPlacement }) {
  const pStatus = deriveStatus(placement.status?.conditions);
  const gpuCount = placement.status?.resources?.gpu?.count;
  const pEndpoint = placement.status?.endpoint?.url;
  const conditions = (placement.status?.conditions ?? []).filter(
    (c) => c.type !== "Ready" && c.type !== "Synced" && c.type !== "Responsive",
  );

  const href = `/placements/${placement.metadata.namespace}/${placement.metadata.name}`;

  return (
    <Link to={href} className="block hover:ring-1 hover:ring-border-hi rounded-xl transition">
      <Card>
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <StatusDot status={pStatus} />
            <span className="text-text font-medium text-sm">{placement.metadata.name}</span>
          </div>
          <div className="flex flex-wrap gap-2 text-xs text-muted">
            <span>
              Env:{" "}
              <span className="text-muted-hi font-mono">
                {placement.spec.inferenceEnvironmentRef.name}
              </span>
            </span>
            {placement.status?.servingProfile && (
              <>
                {placement.status.servingProfile.engine?.name && (
                  <Badge variant="cyan">{placement.status.servingProfile.engine.name}</Badge>
                )}
              </>
            )}
            {gpuCount !== undefined && (
              <Badge variant="neutral">
                {gpuCount} GPU{gpuCount !== 1 ? "s" : ""}
              </Badge>
            )}
            {placement.spec.scaling && (
              <Badge variant={placement.spec.scaling.signal === "Concurrency" ? "purple" : "neutral"}>
                {placement.spec.scaling.signal === "Concurrency" && placement.spec.scaling.concurrency
                  ? `${placement.spec.scaling.concurrency.minReplicas ?? 1}–${placement.spec.scaling.concurrency.maxReplicas}`
                  : placement.spec.scaling.fixed
                    ? `${placement.spec.scaling.fixed.replicas}`
                    : "1"}{" "}
                replica{(placement.spec.scaling.fixed?.replicas ?? 2) !== 1 ? "s" : ""}
              </Badge>
            )}
          </div>
          {conditions.length > 0 && (
            <div className="pt-1">
              <ConditionList conditions={conditions} />
            </div>
          )}
          {pEndpoint && (
            <p className="text-xs font-mono text-muted truncate" title={pEndpoint}>
              {pEndpoint}
            </p>
          )}
        </div>
      </Card>
    </Link>
  );
}
