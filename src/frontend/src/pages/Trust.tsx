import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ErrorBar, Legend, ResponsiveContainer, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { Metrics, Rate, SplitMetrics } from "../api/types";
import { AXIS, Figure, SimpleTable } from "../components/ChartBits";
import { useMeta } from "../components/Layout";
import { EmptyState, ErrorBox, FixtureBanner, Loading, PageTitle } from "../components/States";
import { ciPct, date, humanize, MUTATION_LABELS, num, pct } from "../lib/format";
import { VERDICTS, VERDICT_ORDER, isVerdict } from "../lib/verdicts";
import { EMPTY } from "./Findings";

const TYPES = ["abstract_number", "checklist_flip", "remove_table", "widen_scope", "delete_errorbars", "all"];

function rateCell(r: Rate | null | undefined): string {
  if (!r || r.rate == null) return "—";
  return `${r.k}/${r.n} = ${pct(r.rate)} (${ciPct(r.ci95)})`;
}

function err(r: Rate | null | undefined): [number, number] {
  if (!r || r.rate == null || !r.ci95) return [0, 0];
  return [r.rate - r.ci95[0], r.ci95[1] - r.rate];
}

function Recall({ m }: { m: Metrics }) {
  const rec = m.pooled.recall;
  const types = TYPES.filter((t) => rec[t]?.onus || rec[t]?.baseline);
  if (!types.length) return null;
  const data = types.map((t) => ({
    name: MUTATION_LABELS[t] ?? t,
    onus: rec[t]?.onus?.rate ?? 0,
    baseline: rec[t]?.baseline?.rate ?? 0,
    onusErr: err(rec[t]?.onus),
    baselineErr: err(rec[t]?.baseline),
  }));
  return (
    <Figure
      title="Does Onus catch planted errors?"
      summary="Known errors are planted in papers (a changed number, a flipped checklist answer, a deleted table, widened wording, deleted error bars). Recall is the share Onus flags, against a baseline that asks one model whether each claim is supported."
      table={
        <SimpleTable
          head={["Planted error", "Onus", "Baseline"]}
          rows={types.map((t) => [MUTATION_LABELS[t] ?? t, rateCell(rec[t]?.onus), rateCell(rec[t]?.baseline)])}
        />
      }
      notProven="Planted errors are edits to the parsed text, so they test checking, not PDF parsing. Real errors can be subtler. Small counts give wide intervals."
    >
      <div style={{ height: types.length * 48 + 60 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart layout="vertical" data={data} margin={{ top: 4, right: 16, bottom: 4, left: 4 }} barGap={2}>
            <CartesianGrid stroke="#f1f5f9" horizontal={false} />
            <XAxis type="number" domain={[0, 1]} tickFormatter={(v: number) => pct(v)} tick={AXIS} />
            <YAxis type="category" dataKey="name" tick={AXIS} width={132} interval={0} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="onus" name="Onus" fill="#0f172a" isAnimationActive={false}>
              <ErrorBar dataKey="onusErr" direction="x" width={4} stroke="#64748b" />
            </Bar>
            <Bar dataKey="baseline" name="Baseline (one model)" fill="#cbd5e1" isAnimationActive={false}>
              <ErrorBar dataKey="baselineErr" direction="x" width={4} stroke="#64748b" />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      {m.pooled.collateral && (
        <p className="mt-2 text-sm text-slate-700">
          Collateral flags (new flags away from the planted error): {rateCell(m.pooled.collateral)}.
        </p>
      )}
    </Figure>
  );
}

function Precision({ m }: { m: Metrics }) {
  const systems = m.labels?.systems;
  if (!systems || !Object.keys(systems).length) {
    return (
      <Figure title="Are the flags right? (hand labels)">
        <p className="text-sm text-slate-600">Hand-label precision appears once the blind labels are scored.</p>
      </Figure>
    );
  }
  const keys = ["all", ...VERDICT_ORDER, "not_supported", "supported"].filter(
    (k, i, a) => a.indexOf(k) === i && Object.values(systems).some((s) => s[k]),
  );
  return (
    <Figure
      title="Are the flags right? (hand labels)"
      summary="A blind, shuffled sample of verdicts from Onus and the baseline, labelled correct, incorrect or unsure by hand. Precision is correct / (correct + incorrect)."
      notProven="One labeller, one sample. Unsure labels are excluded from precision and reported separately."
    >
      <div className="overflow-x-auto">
        <SimpleTable
          head={["Verdict", ...Object.keys(systems).map((s) => humanize(s))]}
          rows={keys.map((k) => [
            k === "all" ? "All" : isVerdict(k) ? VERDICTS[k].label : humanize(k),
            ...Object.values(systems).map((s) => {
              const p = s[k];
              return p ? `${pct(p.precision)} (${ciPct(p.ci95)}); ${p.correct}✓ ${p.incorrect}✗ ${p.unsure}?` : "—";
            }),
          ])}
        />
      </div>
    </Figure>
  );
}

function Stability({ name, s }: { name: string; s: SplitMetrics }) {
  const st = s.stability;
  if (!st || st.runs < 2 || !st.matrix) return null;
  const cols = VERDICT_ORDER.filter((v) => Object.values(st.matrix!).some((row) => row[v]));
  const rows = VERDICT_ORDER.filter((v) => st.matrix![v]);
  return (
    <div className="mt-4">
      <h3 className="text-sm font-medium">
        {name}: {st.claims} claims in {st.papers} papers, {st.runs} runs each
      </h3>
      <p className="mt-1 text-sm text-slate-700">
        Full agreement {rateCell(st.full_agreement)}; mean agreement {pct(st.mean_agreement)}.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="text-sm">
          <caption className="mb-1 text-left text-xs text-slate-500">Rows: final verdict. Columns: verdict in each run.</caption>
          <thead>
            <tr>
              <td />
              {cols.map((c) => (
                <th key={c} scope="col" className="px-2 py-1 text-xs font-medium text-slate-600">
                  {VERDICTS[c].label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r}>
                <th scope="row" className="py-1 pr-2 text-left text-xs font-medium text-slate-600">
                  {VERDICTS[r].label}
                </th>
                {cols.map((c) => (
                  <td key={c} className={`px-2 py-1 text-right tabular-nums ${r === c ? "font-medium" : "text-slate-600"}`}>
                    {st.matrix![r]?.[c] ?? 0}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CodeAgreement({ name, s }: { name: string; s: SplitMetrics }) {
  const c = s.code_agreement;
  if (!c) return null;
  if (c.na || !c.confusion) {
    return (
      <p className="mt-2 text-sm text-slate-600">
        {name}: {c.na ?? "not available"} ({c.labelled ?? 0} papers with audit labels; {c.excluded ?? 0} excluded as
        ERROR or no codebase).
      </p>
    );
  }
  const k = c.confusion;
  return (
    <div className="mt-3">
      <h3 className="text-sm font-medium">
        {name}: accuracy {pct(c.accuracy)}, Cohen's κ {num(c.kappa)}, n = {c.n}
      </h3>
      <table className="mt-2 text-sm">
        <thead>
          <tr>
            <td />
            <th scope="col" className="px-3 py-1 text-xs font-medium text-slate-600">Audit: mismatch</th>
            <th scope="col" className="px-3 py-1 text-xs font-medium text-slate-600">Audit: passed</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row" className="py-1 pr-3 text-left text-xs font-medium text-slate-600">Onus: mismatch</th>
            <td className="px-3 py-1 text-right tabular-nums">{k.pred_mismatch_audit_mismatch}</td>
            <td className="px-3 py-1 text-right tabular-nums">{k.pred_mismatch_audit_passed}</td>
          </tr>
          <tr>
            <th scope="row" className="py-1 pr-3 text-left text-xs font-medium text-slate-600">Onus: consistent</th>
            <td className="px-3 py-1 text-right tabular-nums">{k.pred_ok_audit_mismatch}</td>
            <td className="px-3 py-1 text-right tabular-nums">{k.pred_ok_audit_passed}</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

const DOC_LABELS: Record<string, string> = {
  "eval_protocol.md": "Evaluation protocol",
  "eval_log.md": "Run log",
};

/** Freeze, eval runs, harness fingerprint and the versions behind the reports. From /api/meta only, so it shows
 * before the held-out metrics exist. */
function VersionsAndRuns() {
  const meta = useMeta();
  if (meta.isPending) return <Loading />;
  if (meta.error) return <ErrorBox error={meta.error} />;
  const m = meta.data;
  const fz = m.freeze;
  const er = m.eval_runs;
  const max = er?.max ?? 3;
  const harness = fz?.harness_fingerprint ?? er?.runs.at(-1)?.harness_fingerprint ?? null;
  const docs = m.public_docs ?? [];
  return (
    <section aria-labelledby="versions-h" className="space-y-4">
      <h2 id="versions-h" className="text-lg font-semibold">
        Frozen version and evaluation runs
      </h2>
      <dl className="grid max-w-prose grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-600">This site runs</dt>
        <dd className="font-mono break-anywhere">
          v{m.onus_version} · {m.fingerprint}
        </dd>
        <dt className="text-slate-600">Frozen</dt>
        <dd className="break-anywhere">
          {fz ? (
            <>
              <span className="font-mono">
                {fz.version ? `v${fz.version} · ` : ""}{fz.fingerprint}
              </span>
              {fz.created_at && <span className="text-slate-600"> on {date(fz.created_at)}</span>}
              {!fz.matches_current && <span className="text-amber-900"> (differs from the running pipeline)</span>}
            </>
          ) : (
            "Not frozen yet."
          )}
        </dd>
        <dt className="text-slate-600">Harness</dt>
        <dd className="font-mono break-anywhere">{harness ?? "—"}</dd>
      </dl>
      <p className="max-w-prose text-sm text-slate-700">
        {er && er.count > 0
          ? `${er.count} of at most ${max} evaluation runs were used on the held-out eval split before the freeze. Each run is logged before it starts.`
          : `At most ${max} evaluation runs are allowed before the freeze. Each run is logged before it starts.`}
        {" "}The pipeline fingerprint hashes the prompts, rubrics, models and pipeline code; the harness fingerprint
        hashes the code that plants errors and scores them.
      </p>
      {er && er.runs.length > 0 && (
        <div className="overflow-x-auto">
          <SimpleTable
            head={["Run", "Started (UTC)", "Commit", "Pipeline", "Harness"]}
            rows={er.runs.map((r) => [r.run ?? "—", r.started ?? "—", r.commit ?? "—", r.pipeline_fingerprint ?? "—", r.harness_fingerprint ?? "—"])}
          />
        </div>
      )}
      {docs.length > 0 && (
        <ul className="flex flex-wrap gap-x-4 text-sm">
          {docs.map((d) => (
            <li key={d}>
              <a href={`/api/public/${d}`} className="text-blue-900 underline">
                {DOC_LABELS[d] ?? d}
              </a>{" "}
              <span className="text-xs text-slate-500">({d}, plain text)</span>
            </li>
          ))}
        </ul>
      )}
      {m.versions && m.versions.length > 0 && (
        <div className="overflow-x-auto">
          <h3 className="text-sm font-medium">Versions behind the reports on this site</h3>
          <SimpleTable
            head={["Version", "Fingerprint", "Mode", "Reports", "First", "Last"]}
            rows={m.versions.map((v) => [
              v.fingerprint === m.fingerprint ? `v${v.onus_version} (current)` : `v${v.onus_version} (older)`,
              v.fingerprint,
              v.mode,
              v.analyses,
              date(v.first_seen) || "—",
              date(v.last_seen) || "—",
            ])}
          />
          <p className="mt-1 text-xs text-slate-500">
            Reports from an older version stay listed and are labelled as such; they are not re-run automatically.
          </p>
        </div>
      )}
    </section>
  );
}

function Body({ m }: { m: Metrics }) {
  const splits = Object.entries(m.splits ?? {});
  return (
    <div className="space-y-10">
      <Recall m={m} />
      <Precision m={m} />
      <Figure
        title="Is it stable?"
        summary="Batch reports run three times; a claim's verdict is the majority, and stability is how many runs agree."
        notProven="Agreement between runs of the same model measures consistency, not correctness."
      >
        {splits.some(([, s]) => (s.stability?.runs ?? 1) > 1) ? (
          splits.map(([n, s]) => <Stability key={n} name={n} s={s} />)
        ) : (
          <p className="text-sm text-slate-600">Stability needs three-run results.</p>
        )}
      </Figure>
      <Figure
        title="Do code checks agree with a manual audit?"
        summary="Paper-level: does Onus find a paper–code mismatch where an earlier manual audit did (PARTIAL, CRITICAL or FAILED), and no mismatch where it PASSED?"
        notProven="The audit is one earlier pass over the same code; ERROR and no-codebase papers are excluded."
      >
        {splits.map(([n, s]) => (
          <CodeAgreement key={n} name={n} s={s} />
        ))}
      </Figure>
      {splits.length > 0 && (
        <Figure title="What these numbers were measured on">
          <div className="overflow-x-auto">
            <SimpleTable
              head={["Measured on", "Pipeline", "Harness", "Plan", "Date"]}
              rows={splits.map(([n, s]) => [n, s.fingerprint ?? "—", s.harness_fingerprint ?? "—", s.mutation_plan ?? "—", date(s.created_at) || "—"])}
            />
          </div>
        </Figure>
      )}
    </div>
  );
}

export default function Trust() {
  const q = useQuery({ queryKey: ["metrics"], queryFn: api.metrics });
  return (
    <div>
      <PageTitle sub="Onus publishes its own error rates: recall on planted errors, precision from blind hand labels, stability across runs, and agreement with a code audit.">
        How well Onus works
      </PageTitle>
      {q.isPending ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : q.data == null ? (
        <EmptyState title={EMPTY}>
          These numbers are measured once, on the frozen version, over held-out papers Onus was not tuned on.
        </EmptyState>
      ) : (
        <>
          <FixtureBanner fixture={q.data._fixture} />
          <Body m={q.data} />
        </>
      )}
      <div className="mt-12 border-t border-slate-200 pt-8">
        <VersionsAndRuns />
      </div>
    </div>
  );
}
