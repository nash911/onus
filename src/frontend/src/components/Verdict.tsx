import type { Verdict } from "../types/result";
import { AUDIT_STATUS, VERDICT_ORDER, VERDICTS, type AuditStatus, type Style } from "../lib/verdicts";

function Badge({ style, children, size = "sm" }: { style: Style; children?: React.ReactNode; size?: "sm" | "md" }) {
  const { Icon } = style;
  const pad = size === "md" ? "px-2 py-1 text-sm" : "px-1.5 py-0.5 text-xs";
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded font-medium ring-1 ring-inset ${pad} ${style.badge}`}
    >
      <Icon className="shrink-0" />
      {style.label}
      {children}
    </span>
  );
}

export function VerdictBadge({ verdict, size }: { verdict: Verdict; size?: "sm" | "md" }) {
  return <Badge style={VERDICTS[verdict]} size={size} />;
}

export function AuditBadge({ status }: { status: string }) {
  const style = AUDIT_STATUS[status as AuditStatus] ?? AUDIT_STATUS.not_checkable;
  return <Badge style={style} />;
}

/** Compact per-verdict counts: icon + number, each with an accessible label; zero counts are omitted. */
export function VerdictCounts({ counts }: { counts: Partial<Record<Verdict, number>> | undefined }) {
  const items = VERDICT_ORDER.filter((v) => (counts?.[v] ?? 0) > 0);
  if (!items.length) return <span className="text-slate-500">—</span>;
  return (
    <ul className="flex flex-wrap gap-x-2.5 gap-y-1 text-sm">
      {items.map((v) => {
        const { Icon, label, fill } = VERDICTS[v];
        return (
          <li key={v} className="inline-flex items-center gap-0.5 tabular-nums" title={label}>
            <Icon style={{ color: fill }} />
            <span className="sr-only">{label}: </span>
            {counts?.[v]}
          </li>
        );
      })}
    </ul>
  );
}

/** A stacked bar of the verdict mix. Decorative: always paired with VerdictCounts or a table. */
export function VerdictMixBar({ counts }: { counts: Partial<Record<Verdict, number>> | undefined }) {
  const total = VERDICT_ORDER.reduce((s, v) => s + (counts?.[v] ?? 0), 0);
  if (!total) return null;
  return (
    <div className="flex h-1.5 w-full overflow-hidden rounded-sm bg-slate-100" aria-hidden="true">
      {VERDICT_ORDER.map((v) =>
        counts?.[v] ? (
          <div key={v} style={{ width: `${(100 * (counts[v] ?? 0)) / total}%`, background: VERDICTS[v].fill }} />
        ) : null,
      )}
    </div>
  );
}

export function VerdictLegend() {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600" aria-label="Verdict legend">
      {VERDICT_ORDER.map((v) => {
        const { Icon, label, fill } = VERDICTS[v];
        return (
          <li key={v} className="inline-flex items-center gap-1">
            <Icon style={{ color: fill }} />
            {label}
          </li>
        );
      })}
    </ul>
  );
}
