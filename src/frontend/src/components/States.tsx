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


export function PageTitle({ children, sub }: { children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <header className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900 break-anywhere">{children}</h1>
      {sub && <div className="mt-2 max-w-prose text-slate-600">{sub}</div>}
    </header>
  );
}

