import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useEffect, useRef } from "react";
import { api } from "../api/client";
import { isOlderVersion } from "../lib/versions";

const NAV = [
  { to: "/corpus", label: "Corpus" },
  { to: "/findings", label: "Findings" },
  { to: "/trust", label: "How well Onus works" },
  { to: "/method", label: "Method" },
];

export function useMeta() {
  return useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: 60_000 });
}

/** Version and pipeline fingerprint, always from /api/meta (they change at the fix pass and the freeze). */
export function VersionBadge({ version, fingerprint, label = "pipeline" }: {
  version?: string;
  fingerprint?: string;
  label?: string;
}) {
  if (!version || !fingerprint) return null;
  return (
    <span className="inline-flex items-center gap-1.5 rounded border border-slate-300 px-1.5 py-0.5 font-mono text-xs text-slate-700">
      <span>v{version}</span>
      <span className="text-slate-400" aria-hidden="true">·</span>
      <span>
        <span className="sr-only">{label} fingerprint </span>
        {fingerprint}
      </span>
    </span>
  );
}

/** Small label for a report made by a pipeline other than the one this app runs. Shown, never hidden. */
export function OlderVersionBadge({ fingerprint, version }: { fingerprint?: string | null; version?: string | null }) {
  const meta = useMeta();
  if (!isOlderVersion(fingerprint, meta.data?.fingerprint)) return null;
  return (
    <span
      className="inline-flex items-center rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-xs text-amber-900"
      title={`Made with ${version ? `v${version}, ` : ""}pipeline ${fingerprint}; this site runs ${meta.data?.fingerprint}.`}
    >
      older version (fingerprint&nbsp;<span className="font-mono">{fingerprint}</span>)
    </span>
  );
}

export function Layout() {
  const meta = useMeta();
  const loc = useLocation();
  const main = useRef<HTMLElement>(null);

  // Move focus to the page on navigation so keyboard and screen-reader users land on the new content.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus();
    window.scrollTo(0, 0);
  }, [loc.pathname]);

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to content
      </a>
      <header className="border-b border-slate-200">
        <div className="mx-auto flex max-w-5xl flex-wrap items-baseline gap-x-6 gap-y-2 px-4 py-3">
          <Link to="/" className="text-lg font-semibold tracking-tight text-slate-900">
            Onus
          </Link>
          <nav aria-label="Main">
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {NAV.map((n) => (
                <li key={n.to}>
                  <NavLink
                    to={n.to}
                    className={({ isActive }) =>
                      isActive ? "font-medium text-slate-900 underline underline-offset-4" : "text-slate-600 hover:text-slate-900"
                    }
                  >
                    {n.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>
      <main id="main" ref={main} tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 focus:outline-none">
        <Outlet />
      </main>
      <footer className="border-t border-slate-200">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-4 text-xs text-slate-500">
          <span className="inline-flex items-center gap-2">
            Onus <VersionBadge version={meta.data?.onus_version} fingerprint={meta.data?.fingerprint} />
          </span>
          <span>Reasoning runs on Berget AI (EU); inputs are not used for training.</span>
          <span>Reports show evidence gaps, not misconduct.</span>
        </div>
      </footer>
    </div>
  );
}
