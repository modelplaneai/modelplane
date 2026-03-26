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
import { ConditionList } from "../../components/ConditionList";
import { EventTimeline } from "../../components/EventTimeline";
import type { ModelPlacement } from "../../api/types";

export function DeploymentDetail() {
  const { ns, name } = useParams<{ ns: string; name: string }>();
  const navigate = useNavigate();
  const api = useApi();
  const { data: deployment, isLoading, error } = useDeployment(ns ?? "", name ?? "");
  const { data: placementsData } = usePlacements(ns ?? "");
  const { data: deploymentEvents } = useEvents(ns ?? "", "ModelDeployment", name ?? "");
  const [showCurl, setShowCurl] = useState(false);
  const [endpointCopied, setEndpointCopied] = useState(false);
  const [deleting, setDeleting] = useState(false);

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
  const modelName = deployment.status?.model?.name ?? deployment.spec.modelRef.name;
  const endpointUrl = deployment.status?.endpoint?.url;
  const age = relativeAge(deployment.metadata.creationTimestamp);
  const conditions = deployment.status?.conditions ?? [];

  // Filter placements belonging to this deployment.
  const placements: ModelPlacement[] = (placementsData?.items ?? []).filter(
    (p) => p.metadata.labels?.["modelplane.ai/deployment"] === name,
  );

  // Collect events for the deployment and all its placements.
  const allEvents = [
    ...(deploymentEvents?.items ?? []),
  ];

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
        </div>
        <Button variant="ghost" onClick={handleDelete} disabled={deleting} className="text-red hover:text-red">
          {deleting ? "Deleting…" : "Delete"}
        </Button>
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
      <div>
        <SectionLabel>ENDPOINT</SectionLabel>
        <Card>
          {endpointUrl ? (
            <div className="space-y-3">
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
            </div>
          ) : (
            <p className="text-muted text-sm">Endpoint not available yet.</p>
          )}
        </Card>
      </div>

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

      {/* Events */}
      {allEvents.length > 0 && (
        <div>
          <SectionLabel>EVENTS</SectionLabel>
          <Card>
            <EventTimeline events={allEvents} />
          </Card>
        </div>
      )}

      {/* Chat */}
      <div>
        <SectionLabel>CHAT</SectionLabel>
        {status === "ready" && endpointUrl ? (
          <ChatWidget namespace={ns ?? ""} deployment={name ?? ""} model={modelName} />
        ) : (
          <Card>
            <p className="text-muted text-sm">Chat is available once the deployment is ready.</p>
          </Card>
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

  return (
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
          {gpuCount !== undefined && (
            <Badge variant="neutral">
              {gpuCount} GPU{gpuCount !== 1 ? "s" : ""}
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
  );
}
