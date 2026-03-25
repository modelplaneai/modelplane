import { Routes, Route, Navigate } from "react-router-dom";
import { NavBar } from "./components/NavBar";
import { ModelsPage } from "./pages/models/ModelsPage";
import { DeploymentsPage } from "./pages/deployments/DeploymentsPage";
import { DeploymentDetail } from "./pages/deployments/DeploymentDetail";
import { EnvironmentsPage } from "./pages/admin/EnvironmentsPage";
import { CatalogPage } from "./pages/admin/CatalogPage";

export default function App() {
  return (
    <>
      <NavBar />
      <main className="flex-1 px-6 py-6">
        <Routes>
          <Route path="/" element={<Navigate to="/models" replace />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/deployments" element={<DeploymentsPage />} />
          <Route
            path="/deployments/:ns/:name"
            element={<DeploymentDetail />}
          />
          <Route
            path="/admin/environments"
            element={<EnvironmentsPage />}
          />
          <Route path="/admin/catalog" element={<CatalogPage />} />
        </Routes>
      </main>
    </>
  );
}
