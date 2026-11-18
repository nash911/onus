import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useLocation, useMatch } from "react-router-dom";
import { useEffect, useRef } from "react";
import { api } from "../api/client";
import { installTracking, referrerHost, screenBucket, track } from "../lib/track";
import { isOlderVersion } from "../lib/versions";
import { AUTHOR_URL, CONTRIBUTE_URL, REPO_URL, StarIcon } from "./Links";
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


export function Layout() {
  const meta = useMeta();
  const loc = useLocation();
  const main = useRef<HTMLElement>(null);
  // the report fills the viewport: two panes that scroll on their own
  const full = !!useMatch("/analyses/:id");
  // the landing page draws its own full-width bands
  const home = !!useMatch("/");

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

  const wrap = full ? "px-4" : "mx-auto w-full max-w-[1120px] px-4 sm:px-6";
  return (
    <div className={`flex flex-col ${full ? "h-dvh overflow-hidden" : "min-h-screen"}`}>
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to content
      </a>
      <header className="shrink-0 border-b border-slate-200 bg-paper">
        <div className={`flex flex-wrap items-center justify-between gap-3 ${full ? "py-1.5" : "py-3"} ${wrap}`}>
          <Link to="/" aria-label="Onus, home" className="rounded">
            <Logo size={full ? 24 : 26} />
          </Link>
          <nav aria-label="Site" className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm font-medium">
            {home && (
              <>
                <a href="#how" className="hidden text-slate-700 hover:text-slate-900 sm:inline">How it works</a>
                <a href="#verdicts" className="hidden text-slate-700 hover:text-slate-900 sm:inline">Verdicts</a>
                <a href="#examples" className="hidden text-slate-700 hover:text-slate-900 sm:inline">Examples</a>
              </>
            )}
            <a
              href={REPO_URL}
              onClick={() => track("outbound_click", { target: "github_header" })}
              className={`inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 font-semibold text-slate-900 hover:bg-slate-50 ${full ? "h-8" : "h-9"}`}
            >
              <StarIcon className="h-3.5 w-3.5" />
              Star on GitHub
            </a>
          </nav>
        </div>
      </header>
      <main
        id="main"
        ref={main}
        tabIndex={-1}
        className={
          full
            ? "min-h-0 w-full flex-1 focus:outline-none"
            : home
              ? "w-full flex-1 focus:outline-none"
              : "mx-auto w-full max-w-[1120px] flex-1 px-4 py-8 focus:outline-none sm:px-6"
        }
      >
        <Outlet />
      </main>
      <footer className="shrink-0 border-t border-slate-200">
        <div className={`flex flex-wrap items-center justify-between gap-x-6 gap-y-2 text-slate-600 ${full ? "py-1.5 text-xs" : "py-6 text-[13px]"} ${wrap}`}>
          <div className="flex flex-wrap items-center gap-3">
            {!full && (
              <Link to="/" aria-label="Onus, home" className="rounded">
                <Logo size={17} />
              </Link>
            )}
            <VersionBadge version={meta.data?.onus_version} fingerprint={meta.data?.fingerprint} />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <a href={REPO_URL} onClick={() => track("outbound_click", { target: "github_footer" })} className="text-slate-700 hover:text-slate-900 hover:underline">
              GitHub
            </a>
            <span aria-hidden="true">·</span>
            <a href={CONTRIBUTE_URL} onClick={() => track("outbound_click", { target: "contribute_footer" })} className="text-slate-700 hover:text-slate-900 hover:underline">
              Contribute
            </a>
            <span aria-hidden="true">·</span>
            <span>
              Built by{" "}
              <a href={AUTHOR_URL} className="text-slate-700 hover:text-slate-900 hover:underline">
                Avinash Ranganath
              </a>
            </span>
          </div>
        </div>
      </footer>
    </div>
  );
}
