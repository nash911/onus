import type { Verdict } from "../types/result";
import { AUDIT_STATUS, VERDICT_ORDER, VERDICTS, type AuditStatus, type Style } from "../lib/verdicts";

/** The Journal chip: tinted ground, hairline border, a dot in the verdict colour, and the name (never colour alone). */
function Badge({ style, children, size = "sm" }: { style: Style; children?: React.ReactNode; size?: "sm" | "md" }) {
  const pad = size === "md" ? "px-2.5 py-1 text-sm" : "px-2 py-0.5 text-[13px]";
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded border font-semibold leading-5 ${pad} ${style.badge}`}>
      <span className="h-2 w-2 shrink-0 rounded-full bg-current" aria-hidden="true" />
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

/** Compact per-verdict counts as small chips (dot + number), each with an accessible label; zero counts are omitted. */
export function VerdictCounts({ counts }: { counts: Partial<Record<Verdict, number>> | undefined }) {
  // All five verdicts, always in the same order, so cards line up and a zero reads as "none" rather than "missing".
  if (!counts || !VERDICT_ORDER.some((v) => (counts[v] ?? 0) > 0)) return <span className="text-slate-500">—</span>;
  return (
    <ul className="flex flex-wrap gap-1.5 text-[13px]">
      {VERDICT_ORDER.map((v) => {
        const { label, badge } = VERDICTS[v];
        const n = counts[v] ?? 0;
        return (
          <li
            key={v}
            className={`inline-flex items-center gap-1 rounded border px-1.5 font-semibold leading-5 tabular-nums ${badge} ${n ? "" : "opacity-45"}`}
            title={`${label}: ${n}`}
          >
            <span className="h-2 w-2 rounded-full bg-current" aria-hidden="true" />
            <span className="sr-only">{label}: </span>
            {n}
          </li>
        );
      })}
    </ul>
  );
}
