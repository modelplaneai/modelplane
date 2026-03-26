import { NavLink, useLocation, Link } from "react-router-dom";
import { useNamespace } from "../hooks/useNamespace";
import { useApi } from "../api/context";
import { useQuery } from "@tanstack/react-query";
import type { KubeList, ObjectMeta } from "../api/types";

const linkClass = ({ isActive }: { isActive: boolean }) =>
  isActive
    ? "text-purple border-b-2 border-purple pb-0.5"
    : "text-muted hover:text-muted-hi";

export function NavBar() {
  const { pathname } = useLocation();
  const isAdmin = pathname.startsWith("/admin");
  const { namespace, setNamespace } = useNamespace();

  const api = useApi();
  const { data: nsData } = useQuery<KubeList<{ metadata: ObjectMeta }>>({
    queryKey: ["namespaces"],
    queryFn: () => api.listNamespaces(),
  });

  // Show namespaces annotated for the UI. The annotation value is the
  // display name (supports spaces and other characters labels don't).
  const ANNOTATION = "ui.modelplane.ai/namespace";
  const namespaces = (nsData?.items ?? [])
    .filter((n) => n.metadata.annotations?.[ANNOTATION])
    .map((n) => ({
      name: n.metadata.name,
      displayName: n.metadata.annotations![ANNOTATION]!,
    }));

  return (
    <nav className="bg-bg-mid border-b border-border h-14 flex items-center px-6 gap-6 shrink-0">
      <Link to="/deployments" className="text-sm font-semibold text-text tracking-wide mr-2 hover:text-purple-hi transition">
        {"\u2708"} Modelplane
      </Link>

      {isAdmin ? (
        <>
          <Link to="/deployments" className="text-muted hover:text-muted-hi text-sm">
            &larr; Back
          </Link>
          <NavLink to="/admin/environments" className={linkClass} end>
            <span className="text-sm">Environments</span>
          </NavLink>
          <NavLink to="/admin/catalog" className={linkClass} end>
            <span className="text-sm">Model Catalog</span>
          </NavLink>
        </>
      ) : (
        <>
          {/* Namespace selector */}
          {namespaces.length > 0 && (
            <select
              value={namespace}
              onChange={(e) => setNamespace(e.target.value)}
              className="bg-bg border border-border rounded-md px-2 py-1 text-xs text-muted-hi focus:outline-none focus:border-border-hi"
            >
              {namespaces.map((n) => (
                <option key={n.name} value={n.name}>
                  {n.displayName}
                </option>
              ))}
            </select>
          )}

          <div className="ml-auto">
            <Link
              to="/admin/environments"
              className="text-2xl text-muted hover:text-muted-hi transition"
              title="Admin"
            >
              &#9881;
            </Link>
          </div>
        </>
      )}
    </nav>
  );
}
