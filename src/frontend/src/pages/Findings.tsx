import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import type { Findings, Rq1Series } from "../api/types";
import { AXIS, CIStrip, Figure, SimpleTable } from "../components/ChartBits";
import { EmptyState, ErrorBox, FixtureBanner, Loading, PageTitle } from "../components/States";
import { ciNum, humanize, num, pct } from "../lib/format";

export const EMPTY = "Results appear after the held-out runs.";

function Rq1Chart({ s, label }: { s: Rq1Series; label: string }) {
  return (
    <div>
      <h3 className="text-sm font-medium text-slate-800">
        {label}: ρ = {num(s.rho)}
        {s.ci95 ? ` (95% CI ${ciNum(s.ci95)})` : ""}, n = {s.n}
      </h3>
      <div className="mt-2 h-60">
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 8, right: 12, bottom: 24, left: 0 }}>
            <CartesianGrid stroke="#f1f5f9" />
            <XAxis
              type="number"
              dataKey="x"
              name="Flagged share"
              tickFormatter={(v: number) => pct(v)}
              tick={AXIS}
              label={{ value: "Share of claims flagged", position: "insideBottom", offset: -12, ...AXIS }}
            />
            <YAxis
              type="number"
              dataKey="y"
              name={label}
              tick={AXIS}
              width={36}
              allowDecimals={false}
              domain={[(min: number) => Math.floor(min), (max: number) => Math.ceil(max)]}
            />
            <Tooltip
              formatter={(v, name) => (name === "Flagged share" ? pct(Number(v), 1) : num(Number(v)))}
              cursor={{ strokeDasharray: "3 3" }}
            />
            <Scatter data={s.points} fill="#0f172a" fillOpacity={0.75} />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function jitter(i: number): number {
  // deterministic spread so overlapping points stay visible
  return (((i * 37) % 11) / 11 - 0.5) * 0.3;
}

function Body({ f }: { f: Findings }) {
  const r1 = f.rq1;
  const r2 = f.rq2;
  const r3 = f.rq3;
  const r4 = f.rq4?.funnel;
  const pp = (x: number) => `${(x * 100).toFixed(1)} pp`;
  return (
    <div className="space-y-10">
      <p className="max-w-prose text-slate-700">
        From {f.papers ?? "?"} papers
        {f.splits ? ` (${Object.entries(f.splits).map(([k, v]) => `${k} ${v}`).join(", ")})` : ""}. Intervals are 95%
        bootstrap intervals{f.bootstrap ? ` (${f.bootstrap} resamples, fixed seed)` : ""}.
      </p>

      {r1 && (r1.ai_score || r1.human_score) && (
        <Figure
          title="RQ1. Do flagged claims track reviewer scores?"
          summary="Each point is a paper: the share of its claims Onus flags against its mean reviewer score."
          table={
            <SimpleTable
              head={["Score", "Spearman ρ", "95% CI", "Papers"]}
              rows={[
                ...(r1.ai_score ? [["AI reviewers", num(r1.ai_score.rho), ciNum(r1.ai_score.ci95), r1.ai_score.n]] : []),
                ...(r1.human_score ? [["Human reviewers", num(r1.human_score.rho), ciNum(r1.human_score.ci95), r1.human_score.n]] : []),
              ]}
            />
          }
          notProven="A correlation between flags and scores does not show that reviewers noticed the same problems, or that flags cause lower scores. Flagged share counts evidence gaps in the paper's own text; it is not an error rate. Human scores exist for fewer papers."
        >
          <div className="grid gap-6 md:grid-cols-2">
            {r1.ai_score && <Rq1Chart s={r1.ai_score} label="Mean AI reviewer score" />}
            {r1.human_score && <Rq1Chart s={r1.human_score} label="Mean human reviewer score" />}
          </div>
        </Figure>
      )}

      {r2 && (
        <Figure
          title="RQ2. Do accepted and rejected papers differ?"
          summary={
            r2.difference != null
              ? `Mean flagged share: accepted ${pct(r2.mean_accepted, 1)} (n = ${r2.n_accepted}), rejected ${pct(r2.mean_rejected, 1)} (n = ${r2.n_rejected}). Difference (rejected − accepted) ${pp(r2.difference)}${r2.ci95 ? `, 95% CI ${pp(r2.ci95[0])} to ${pp(r2.ci95[1])}` : ""}; Mann–Whitney p = ${num(r2.mann_whitney_p, 3)}.`
              : "Not enough papers in both groups."
          }
          table={
            <SimpleTable
              head={["Group", "Papers", "Mean flagged share"]}
              rows={[
                ["Accepted", r2.n_accepted, pct(r2.mean_accepted, 1)],
                ["Rejected", r2.n_rejected, pct(r2.mean_rejected, 1)],
              ]}
            />
          }
          notProven="Acceptance depends on much more than evidence gaps. A difference here does not mean Onus can predict decisions, and no difference does not mean decisions ignore evidence. The sets are balanced 50/50 by design, unlike the full conference."
        >
          {r2.series && (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <ScatterChart margin={{ top: 8, right: 12, bottom: 8, left: 0 }}>
                  <CartesianGrid stroke="#f1f5f9" />
                  <XAxis
                    type="number"
                    dataKey="x"
                    domain={[-0.5, 1.5]}
                    ticks={[0, 1]}
                    tickFormatter={(v: number) => (v === 0 ? "Accepted" : "Rejected")}
                    tick={AXIS}
                  />
                  <YAxis type="number" dataKey="y" tickFormatter={(v: number) => pct(v)} tick={AXIS} width={44} />
                  <Scatter
                    data={[
                      ...r2.series.accepted.map((y, i) => ({ x: 0 + jitter(i), y })),
                      ...r2.series.rejected.map((y, i) => ({ x: 1 + jitter(i + 5), y })),
                    ]}
                    fill="#0f172a"
                    fillOpacity={0.7}
                  />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          )}
          {r2.difference != null && r2.ci95 && (
            <div className="mt-2">
              <p className="text-xs text-slate-500">Difference with 95% interval</p>
              <CIStrip
                value={r2.difference}
                ci={r2.ci95}
                min={Math.min(-0.2, r2.ci95[0])}
                max={Math.max(0.2, r2.ci95[1])}
                format={(x: number) => `${(x * 100).toFixed(0)} pp`}
              />
            </div>
          )}
        </Figure>
      )}

      {r3 && r3.items.length > 0 && (
        <Figure
          title="RQ3. How often is a “Yes” on the checklist not backed by the paper?"
          summary="For each checklist item: the share of papers answering “Yes” where the paper does not back the answer (unbacked) or contradicts it."
          table={
            <SimpleTable
              head={["Item", "Yes answers", "Unbacked", "Contradicted", "Share not backed"]}
              rows={r3.items.map((it) => [humanize(it.key), it.yes, it.unbacked, it.contradicted ?? 0, pct(it.unbacked_share)])}
            />
          }
          notProven="An unbacked answer means Onus did not find support in the PDF; the support may be in supplementary material Onus did not read. Items are checked by different rules, so shares are not comparable as a ranking of honesty."
        >
          <div style={{ height: Math.max(160, r3.items.length * 30 + 40) }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={[...r3.items].sort((a, b) => (b.unbacked_share ?? 0) - (a.unbacked_share ?? 0)).map((it) => ({
                  name: humanize(it.key),
                  share: it.unbacked_share ?? 0,
                  label: `${it.unbacked + (it.contradicted ?? 0)}/${it.yes}`,
                }))}
                margin={{ top: 4, right: 44, bottom: 4, left: 4 }}
              >
                <CartesianGrid stroke="#f1f5f9" horizontal={false} />
                <XAxis type="number" domain={[0, 1]} tickFormatter={(v: number) => pct(v)} tick={AXIS} />
                <YAxis type="category" dataKey="name" tick={AXIS} width={128} interval={0} />
                <Bar dataKey="share" fill="#475569" isAnimationActive={false}>
                  <LabelList dataKey="label" position="right" style={AXIS} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Figure>
      )}

      {r4 && (
        <Figure
          title="RQ4. How many results can be re-run?"
          summary={r4.note}
          table={
            <SimpleTable
              head={["Step", "Papers"]}
              rows={[
                ["All papers", r4.papers],
                ["Code submitted", r4.with_code],
                ["Code executes", r4.executable],
                ["Re-run attempted", r4.rerun_attempted],
                ["Re-run consistent with the claim", r4.consistent],
              ]}
            />
          }
          notProven="A failed or missing re-run does not mean a result is wrong: environments, data access and compute limits all stop re-runs. A consistent re-run checks the reported numbers, not the method's validity."
        >
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                layout="vertical"
                data={[
                  { name: "All papers", n: r4.papers },
                  { name: "Code submitted", n: r4.with_code },
                  { name: "Code executes", n: r4.executable },
                  { name: "Re-run attempted", n: r4.rerun_attempted },
                  { name: "Consistent", n: r4.consistent },
                ]}
                margin={{ top: 4, right: 36, bottom: 4, left: 4 }}
              >
                <XAxis type="number" allowDecimals={false} tick={AXIS} />
                <YAxis type="category" dataKey="name" tick={AXIS} width={128} interval={0} />
                <Bar dataKey="n" fill="#0f172a" isAnimationActive={false}>
                  <LabelList dataKey="n" position="right" style={AXIS} />
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Figure>
      )}
    </div>
  );
}

export default function FindingsPage() {
  const q = useQuery({ queryKey: ["findings"], queryFn: api.findings });
  return (
    <div>
      <PageTitle sub="What the analysed corpus shows about AI-written papers, each with what it does not prove.">Findings</PageTitle>
      {q.isPending ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : q.data == null ? (
        <EmptyState title={EMPTY}>
          The findings are computed once, on the frozen version, over papers Onus was not tuned on.
        </EmptyState>
      ) : (
        <>
          <FixtureBanner fixture={q.data._fixture} />
          <Body f={q.data} />
        </>
      )}
    </div>
  );
}
