import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useLocation, useMatch } from "react-router-dom";
import { useEffect, useRef } from "react";
import { api } from "../api/client";
import { installTracking, referrerHost, screenBucket, track } from "../lib/track";
import { isOlderVersion } from "../lib/versions";
import { Logo } from "./Logo";

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

const PAPER_URL = "https://github.com/nash911/onus/blob/main/paper/onus.pdf";
const CODE_URL = "https://github.com/nash911/onus";

export function Layout() {
  const meta = useMeta();
  const loc = useLocation();
  const main = useRef<HTMLElement>(null);
  // the report fills the viewport: two panes that scroll on their own
  const full = !!useMatch("/analyses/:id");

  // Analytics: one page_view per route (the referrer only on the first page of the visit)
  const firstView = useRef(true);
  useEffect(() => {
    installTracking();
    const route = /^\/analyses\/[^/]+/.test(loc.pathname) ? "/analyses/:id" : loc.pathname;
    track("page_view", {
      route,
      referrer: firstView.current ? referrerHost(document.referrer, window.location.hostname) : undefined,
      screen: screenBucket(window.innerWidth),
    });
    firstView.current = false;
  }, [loc.pathname]);

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
    <div className={`flex flex-col ${full ? "h-dvh overflow-hidden" : "min-h-screen"}`}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to content
      </a>
      <header className="shrink-0 border-b border-slate-200 bg-paper">
        <div className={`flex items-center px-4 py-2 ${full ? "" : "mx-auto max-w-5xl"}`}>
          <Link to="/" aria-label="Onus, home" className="rounded">
            <Logo size={26} />
          </Link>
        </div>
      </header>
      <main
        id="main"
        ref={main}
        tabIndex={-1}
        className={full ? "min-h-0 w-full flex-1 focus:outline-none" : "mx-auto w-full max-w-5xl flex-1 px-4 py-8 focus:outline-none"}
      >
        <Outlet />
      </main>
      <footer className="shrink-0 border-t border-slate-200">
        <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5 text-xs text-slate-600 ${full ? "" : "mx-auto max-w-5xl"}`}>
          <VersionBadge version={meta.data?.onus_version} fingerprint={meta.data?.fingerprint} />
          <a href={PAPER_URL} className="hover:text-slate-900 hover:underline">Paper</a>
          <a href={CODE_URL} className="hover:text-slate-900 hover:underline">Code</a>
          <a href="https://avinashranganath.com" className="hover:text-slate-900 hover:underline">Made by Avinash Ranganath</a>
          <span className="text-slate-500" title="Anonymous usage counts (pages, clicks, time on page) to improve Onus. No cookies; turned off when your browser sends Do Not Track or Global Privacy Control.">
            No cookies; anonymous usage stats
          </span>
        </div>
      </footer>
    </div>
  );
}
