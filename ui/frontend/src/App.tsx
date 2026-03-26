import { useState } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import { NavBar } from "./components/NavBar";
import { DeploymentsPage } from "./pages/deployments/DeploymentsPage";
import { DeploymentDetail } from "./pages/deployments/DeploymentDetail";
import { DeployPage } from "./pages/deploy/DeployPage";
import { PlacementDetail } from "./pages/placements/PlacementDetail";
import { InfrastructurePage } from "./pages/admin/InfrastructurePage";
import { EnvironmentDetail } from "./pages/admin/EnvironmentDetail";
import { CatalogPage } from "./pages/admin/CatalogPage";
import { NamespaceContext } from "./hooks/useNamespace";
import { DEFAULT_NAMESPACE } from "./lib/config";

export default function App() {
  const [namespace, setNamespace] = useState(DEFAULT_NAMESPACE);

  return (
    <NamespaceContext.Provider value={{ namespace, setNamespace }}>
      <NavBar />
      <main className="flex-1 px-6 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/deployments" replace />} />
          <Route path="/deployments" element={<DeploymentsPage />} />
          <Route
            path="/deployments/:ns/:name"
            element={<DeploymentDetail />}
          />
          <Route path="/deploy" element={<DeployPage />} />
          <Route
            path="/placements/:ns/:name"
            element={<PlacementDetail />}
          />
          <Route
            path="/admin/environments"
            element={<InfrastructurePage />}
          />
          <Route
            path="/admin/environments/:name"
            element={<EnvironmentDetail />}
          />
          <Route path="/admin/catalog" element={<CatalogPage />} />
        </Routes>
      </main>
    </NamespaceContext.Provider>
  );
}
