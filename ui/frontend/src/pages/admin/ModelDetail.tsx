import { Link, useParams } from "react-router-dom";
import { useModels } from "../../hooks/useModels";
import { deriveStatus } from "../../lib/status";
import { relativeAge, modelDisplayName } from "../../lib/format";
import { SectionLabel } from "../../components/SectionLabel";
import { StatusDot } from "../../components/StatusDot";
import { Card } from "../../components/Card";
import { Badge } from "../../components/Badge";
import { Labels } from "../../components/Labels";
import { ConditionList } from "../../components/ConditionList";

export function ModelDetail() {
  const { name } = useParams<{ name: string }>();
  const { data, isLoading } = useModels();

  const model = (data?.items ?? []).find((m) => m.metadata.name === name);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-muted text-sm animate-pulse">Loading model…</span>
      </div>
    );
  }

  if (!model) {
    return (
      <div className="flex items-center justify-center py-24">
        <span className="text-red text-sm">Model not found</span>
      </div>
    );
  }

  const status = deriveStatus(model.status?.conditions);
  const conditions = model.status?.conditions ?? [];
  const age = relativeAge(model.metadata.creationTimestamp);
  const serving = model.spec.serving ?? [];

  return (
    <div className="space-y-8">
      {/* Breadcrumb */}
      <div className="text-xs text-muted">
        <Link to="/admin/catalog" className="hover:text-text transition-colors">
          Model Catalog
        </Link>
        <span className="mx-1.5">/</span>
        <span className="text-text">{name}</span>
      </div>

      {/* Header */}
      <div>
        <div className="flex items-center gap-3 mb-2">
          <StatusDot status={status} />
          <h1 className="text-xl text-text font-medium">{name}</h1>
          {age && <span className="text-xs text-muted">{age}</span>}
        </div>
        <p className="text-sm text-muted-hi ml-5">
          {modelDisplayName(model.spec.model.name)}
        </p>
        <Labels labels={model.metadata.labels} className="mt-3 ml-5" />
      </div>

      {/* Model info */}
      <div>
        <SectionLabel>Model</SectionLabel>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card>
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-1">Name</p>
            <p className="text-sm text-text font-mono">{model.spec.model.name}</p>
          </Card>
          <Card>
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-1">Source</p>
            <p className="text-sm text-text">{model.spec.source}</p>
            {model.spec.huggingFace?.repo && (
              <p className="text-xs text-muted mt-1 font-mono">{model.spec.huggingFace.repo}</p>
            )}
          </Card>
          <Card>
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-1">VRAM</p>
            <p className="text-sm text-text font-mono">{model.spec.resources.vram}</p>
          </Card>
          <Card>
            <p className="font-mono text-[11px] uppercase tracking-wider text-muted mb-1">Resources</p>
            <div className="text-sm text-text font-mono space-y-0.5">
              {model.spec.resources.cpu && <p>{model.spec.resources.cpu} CPU</p>}
              {model.spec.resources.memory && <p>{model.spec.resources.memory} mem</p>}
            </div>
          </Card>
        </div>
      </div>

      {/* Serving profiles */}
      <div>
        <SectionLabel>Serving Profiles</SectionLabel>
        {serving.length === 0 ? (
          <p className="text-sm text-muted">No serving profiles configured</p>
        ) : (
          <div className="space-y-3">
            {serving.map((p) => (
              <Card key={p.name}>
                <div className="flex items-center gap-3 mb-3">
                  <p className="text-text font-medium text-sm">{p.name}</p>
                  {p.engine?.name && <Badge variant="cyan">{p.engine.name}</Badge>}
                </div>
                {p.engine && (
                  <div className="space-y-2 text-sm">
                    <div>
                      <span className="text-muted">Image: </span>
                      <span className="text-muted-hi font-mono">{p.engine.image}</span>
                    </div>
                    {p.engine.args && p.engine.args.length > 0 && (
                      <div>
                        <span className="text-muted">Args:</span>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {p.engine.args.map((arg, i) => (
                            <span
                              key={i}
                              className="text-xs font-mono text-muted-hi bg-bg-mid px-2 py-0.5 rounded-md border border-border"
                            >
                              {arg}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {p.environmentSelector?.matchLabels && (
                      <div>
                        <span className="text-muted">Environment selector: </span>
                        <span className="text-muted-hi font-mono">
                          {Object.entries(p.environmentSelector.matchLabels)
                            .map(([k, v]) => `${k}=${v}`)
                            .join(", ")}
                        </span>
                      </div>
                    )}
                  </div>
                )}
              </Card>
            ))}
          </div>
        )}
      </div>

      {/* Conditions */}
      {conditions.length > 0 && (
        <div>
          <SectionLabel>Conditions</SectionLabel>
          <ConditionList conditions={conditions} />
        </div>
      )}
    </div>
  );
}
