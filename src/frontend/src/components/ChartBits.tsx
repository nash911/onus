/** Pieces shared by the chart pages: a figure frame with an accessible data table, and a CI strip. */
import type { CI } from "../api/types";

export function Figure({
  title,
  summary,
  children,
  table,
  notProven,
}: {
  title: string;
  summary?: React.ReactNode;
  children: React.ReactNode;
  table?: React.ReactNode;
  notProven?: React.ReactNode;
}) {
  return (
    <section className="border-t border-slate-200 pt-6">
      <h2 className="text-lg font-semibold">{title}</h2>
      {summary && <p className="mt-1 max-w-prose text-slate-700">{summary}</p>}
      <figure className="mt-4" aria-label={title}>
        <div aria-hidden="true">{children}</div>
        {table && (
          <details className="mt-2 text-sm">
            <summary className="cursor-pointer text-blue-900">Data as a table</summary>
            <div className="mt-2 overflow-x-auto">{table}</div>
          </details>
        )}
      </figure>
      {notProven && (
        <aside className="mt-3 max-w-prose border-l-2 border-amber-400 bg-amber-50/50 px-3 py-2 text-sm text-slate-700">
          <span className="font-medium text-slate-900">What this does not prove. </span>
          {notProven}
        </aside>
      )}
    </section>
  );
}

export function SimpleTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <table className="w-full text-left text-sm">
      <thead className="border-b border-slate-300 text-slate-600">
        <tr>
          {head.map((h) => (
            <th key={h} scope="col" className="py-1.5 pr-3 font-medium">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((c, j) =>
              j === 0 ? (
                <th key={j} scope="row" className="py-1.5 pr-3 font-normal">
                  {c}
                </th>
              ) : (
                <td key={j} className="py-1.5 pr-3 tabular-nums">
                  {c}
                </td>
              ),
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A horizontal estimate with its 95% interval on a fixed axis, with a zero line. Decorative; text says it too. */
export function CIStrip({
  value,
  ci,
  min,
  max,
  format,
}: {
  value: number;
  ci: CI;
  min: number;
  max: number;
  format: (x: number) => string;
}) {
  const W = 320;
  const x = (v: number) => 16 + ((Math.min(max, Math.max(min, v)) - min) / (max - min)) * (W - 32);
  return (
    <svg viewBox={`0 0 ${W} 56`} className="h-14 w-full max-w-md" role="presentation">
      <line x1={16} x2={W - 16} y1={28} y2={28} stroke="#e2e8f0" />
      {min < 0 && max > 0 && <line x1={x(0)} x2={x(0)} y1={12} y2={44} stroke="#94a3b8" strokeDasharray="3 3" />}
      {ci && <line x1={x(ci[0])} x2={x(ci[1])} y1={28} y2={28} stroke="#0f172a" strokeWidth={2} />}
      {ci && <line x1={x(ci[0])} x2={x(ci[0])} y1={22} y2={34} stroke="#0f172a" strokeWidth={2} />}
      {ci && <line x1={x(ci[1])} x2={x(ci[1])} y1={22} y2={34} stroke="#0f172a" strokeWidth={2} />}
      <circle cx={x(value)} cy={28} r={5} fill="#0f172a" />
      <text x={16} y={54} fontSize={10} fill="#64748b">
        {format(min)}
      </text>
      <text x={W - 16} y={54} fontSize={10} fill="#64748b" textAnchor="end">
        {format(max)}
      </text>
      {min < 0 && max > 0 && (
        <text x={x(0)} y={10} fontSize={10} fill="#64748b" textAnchor="middle">
          0
        </text>
      )}
    </svg>
  );
}

export const AXIS = { fontSize: 12, fill: "#475569" };
