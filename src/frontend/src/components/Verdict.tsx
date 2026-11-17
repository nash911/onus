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
