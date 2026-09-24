import { useState } from "react";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/context";
import { Button } from "./ui";

const NAV = [
  { to: "/", label: "Dashboard" },
  { to: "/contacts", label: "Contacts" },
  { to: "/action-items", label: "Action items" },
  { to: "/knowledge", label: "Knowledge" },
  { to: "/calendar", label: "Calendar" },
];

export function AppLayout() {
  const { session, logout } = useAuth();
  const [signingOut, setSigningOut] = useState(false);

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex min-w-0 items-center gap-6">
            <span className="truncate text-sm font-semibold text-slate-900">
              {session?.company.name}
            </span>
            <nav className="flex gap-4 overflow-x-auto text-sm" aria-label="Main">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) =>
                    isActive ? "font-medium text-slate-900" : "text-slate-500 hover:text-slate-900"
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-slate-600 sm:inline">{session?.user.full_name}</span>
            <Button
              variant="secondary"
              disabled={signingOut}
              onClick={async () => {
                setSigningOut(true);
                try {
                  await logout();
                } finally {
                  setSigningOut(false);
                }
              }}
            >
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
