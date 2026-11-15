import { IconSpinner } from "./Icons";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="flex items-center gap-2 py-8 text-slate-600" role="status">
      <IconSpinner /> {label}
    </p>
  );
}

export function ErrorBox({ error, title = "Something went wrong" }: { error: unknown; title?: string }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="my-6 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900" role="alert">
      <p className="font-medium">{title}</p>
      <p className="mt-1 break-anywhere">{message}</p>
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="my-8 border-l-2 border-slate-300 py-2 pl-4">
      <p className="text-lg font-medium text-slate-800">{title}</p>
      {children && <div className="mt-2 max-w-prose text-slate-600">{children}</div>}
    </div>
  );
}

/** Shown on Findings and Trust when the data comes from the dev-only fixture switch. Never in a build. */
export function FixtureBanner({ fixture }: { fixture?: string }) {
  if (!import.meta.env.DEV || !fixture) return null;
  return (
    <div className="my-4 rounded border-2 border-dashed border-fuchsia-500 px-4 py-2 text-sm text-fuchsia-900" role="note">
      <strong>Development fixture ({fixture}).</strong> These are dev-split numbers loaded for building the charts.
      They are not results and never appear in a build.
    </div>
  );
}

export function PageTitle({ children, sub }: { children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900 break-anywhere">{children}</h1>
      {sub && <div className="mt-2 max-w-prose text-slate-600">{sub}</div>}
    </header>
  );
}

export function NotProven({ children }: { children: React.ReactNode }) {
  return (
    <aside className="mt-3 border-l-2 border-amber-400 bg-amber-50/50 px-3 py-2 text-sm text-slate-700">
      <span className="font-medium text-slate-900">What this does not prove. </span>
      {children}
    </aside>
  );
}
