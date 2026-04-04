import { Link } from "react-router-dom";
import { useModels } from "../../hooks/useModels";
import { SectionLabel } from "../../components/SectionLabel";

export function CatalogPage() {
  const { data, isLoading, error } = useModels();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-muted">
        Loading models…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-20 text-red">
        Failed to load models:{" "}
        {error instanceof Error ? error.message : "Unknown error"}
      </div>
    );
  }

  const models = data?.items ?? [];

  return (
    <div>
      <div className="mb-4">
        <SectionLabel>Model Catalog</SectionLabel>
        <p className="text-sm text-muted -mt-2">
          ClusterModels registered by the platform team
        </p>
      </div>

      <table className="w-full mt-4">
        <thead>
          <tr className="font-mono text-[11px] uppercase tracking-wider text-muted">
            <th className="text-left px-4 py-2 font-normal">Name</th>
            <th className="text-left px-4 py-2 font-normal">Model</th>
            <th className="text-left px-4 py-2 font-normal">Backends</th>
            <th className="text-left px-4 py-2 font-normal">VRAM</th>
          </tr>
        </thead>
        <tbody>
          {models.length === 0 && (
            <tr>
              <td
                colSpan={4}
                className="px-4 py-8 text-center text-sm text-muted"
              >
                No models registered
              </td>
            </tr>
          )}
          {models.map((m) => {
            const name = m.metadata.name;
            return (
              <tr key={name} className="border-b border-border hover:bg-bg-card-hover transition-colors cursor-pointer">
                <td className="px-4 py-3 text-sm text-text">
                  <Link to={`/admin/catalog/${name}`} className="hover:text-cyan transition-colors">
                    {name}
                  </Link>
                </td>
                <td className="px-4 py-3 text-sm text-muted-hi">
                  {m.spec.model.name}
                </td>
                <td className="px-4 py-3 text-sm">
                  <div className="flex flex-wrap gap-1">
                    {(m.spec.serving ?? []).map((p) => (
                      <span key={p.name} className="text-xs font-mono text-cyan bg-cyan/10 px-1.5 py-0.5 rounded">
                        {p.engine?.name ?? p.backend}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-4 py-3 text-sm font-mono text-muted-hi">
                  {m.spec.resources.vram}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
