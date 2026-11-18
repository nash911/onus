/** WAI-ARIA tabs: arrow keys, Home and End move between tabs; only the active tab is in the tab order. */
import { useRef } from "react";

export interface TabDef {
  id: string;
  label: string;
  count?: number;
}

export function Tabs({
  tabs,
  active,
  onChange,
  idPrefix,
}: {
  tabs: TabDef[];
  active: string;
  onChange: (id: string) => void;
  idPrefix: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (i: number) => {
    const n = tabs.length;
    const j = ((i % n) + n) % n;
    const t = tabs[j];
    if (!t) return;
    onChange(t.id);
    refs.current[j]?.focus();
  };
  return (
    <div
      role="tablist"
      aria-label="Report sections"
      // One line at every width: the row scrolls sideways if it ever has to. The bottom rule is an inset shadow,
      // not a border, so the active tab's underline is not clipped by the scroll box.
      className="flex gap-x-0 overflow-x-auto sm:gap-x-1 overflow-y-hidden shadow-[inset_0_-1px_0_var(--color-slate-200)] [scrollbar-width:thin]"
    >
      {tabs.map((t, i) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            role="tab"
            id={`${idPrefix}-tab-${t.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${t.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight") move(i + 1);
              else if (e.key === "ArrowLeft") move(i - 1);
              else if (e.key === "Home") move(0);
              else if (e.key === "End") move(tabs.length - 1);
              else return;
              e.preventDefault();
            }}
            className={`shrink-0 whitespace-nowrap border-b-2 px-1.5 py-2 text-[13px] sm:px-3 sm:text-sm ${
              selected
                ? "border-slate-900 font-medium text-slate-900"
                : "border-transparent text-slate-600 hover:text-slate-900"
            }`}
          >
            {t.label}
            {t.count != null && <span className="ml-1 tabular-nums text-slate-500">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  id,
  active,
  idPrefix,
  children,
}: {
  id: string;
  active: string;
  idPrefix: string;
  children: React.ReactNode;
}) {
  if (id !== active) return null;
  return (
    <div role="tabpanel" id={`${idPrefix}-panel-${id}`} aria-labelledby={`${idPrefix}-tab-${id}`} tabIndex={0} className="pt-5 focus-visible:outline-offset-4">
      {children}
    </div>
  );
}
