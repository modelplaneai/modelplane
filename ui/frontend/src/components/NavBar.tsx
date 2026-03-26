import { NavLink, useLocation, Link } from "react-router-dom";

const linkClass = ({ isActive }: { isActive: boolean }) =>
  isActive
    ? "text-purple border-b-2 border-purple pb-0.5"
    : "text-muted hover:text-muted-hi";

export function NavBar() {
  const { pathname } = useLocation();
  const isAdmin = pathname.startsWith("/admin");

  return (
    <nav className="bg-bg-mid border-b border-border h-14 flex items-center px-6 gap-6 shrink-0">
      <span className="text-sm font-semibold text-text tracking-wide mr-2">
        {"\u2708"} Modelplane
      </span>

      {isAdmin ? (
        <>
          <Link to="/models" className="text-muted hover:text-muted-hi text-sm">
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
          <NavLink to="/models" className={linkClass} end>
            <span className="text-sm">Models</span>
          </NavLink>
          <NavLink to="/deployments" className={linkClass} end>
            <span className="text-sm">Deployments</span>
          </NavLink>
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
