import { NavLink, Navigate, Outlet, useNavigate } from "react-router";
import { ApiError } from "../api/client";
import { useLogout, useMe } from "../api/hooks";
import { Disclaimer, PaperBanner } from "../components/Chrome";
import { StatusBar } from "../components/StatusBar";
import { NAV } from "./nav";

export function Layout() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();

  if (me.isPending) return <div className="p-6 text-muted">Loading…</div>;
  if (me.error) {
    if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to="/login" replace />;
    return <div className="p-6 text-danger">Cannot reach the API: {me.error.message}</div>;
  }

  return (
    <div className="flex min-h-screen flex-col">
      <PaperBanner />
      <header className="flex items-center gap-3 border-b border-border bg-surface px-4 py-2">
        <span className="font-semibold">Intraday</span>
        <span className="ml-auto text-sm text-muted">{me.data.email}</span>
        <button
          type="button"
          className="text-sm text-muted underline-offset-2 hover:text-text hover:underline"
          onClick={() => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) })}
        >
          Sign out
        </button>
      </header>
      <StatusBar />
      <div className="flex flex-1">
        <nav aria-label="Main" className="hidden w-48 shrink-0 border-r border-border bg-surface py-2 md:block">
          {NAV.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              end={item.path === "/"}
              className={({ isActive }) =>
                `flex items-center justify-between px-4 py-1.5 text-sm ${
                  isActive ? "bg-surface-2 font-semibold text-text" : "text-muted hover:text-text"
                }`
              }
            >
              {item.label}
              {item.phase > 6 && <span className="text-[10px] text-muted/70">P{item.phase}</span>}
            </NavLink>
          ))}
        </nav>
        <main className="min-w-0 flex-1 p-4 pb-20 md:pb-4">
          <Outlet />
        </main>
      </div>
      <Disclaimer />
      <nav
        aria-label="Mobile"
        className="fixed inset-x-0 bottom-0 z-20 flex justify-around border-t border-border bg-surface py-1 md:hidden"
      >
        {NAV.filter((n) => n.mobile).map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            end={item.path === "/"}
            className={({ isActive }) => `px-2 py-1 text-xs ${isActive ? "font-semibold text-accent" : "text-muted"}`}
          >
            {item.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
