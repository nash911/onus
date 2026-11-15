import { useMeta, VersionBadge } from "../components/Layout";
import { PageTitle } from "../components/States";
import { VERDICTS, VERDICT_ORDER } from "../lib/verdicts";
import { VerdictBadge } from "../components/Verdict";

const ROLES: Record<string, string> = {
  extract: "Claim extraction, evidence selection, mapping numbers to table cells",
  judge: "Adjudication of flagged claims, scope conflicts with limitations",
  fast: "Short classifications (claim scope)",
  embed: "Retrieval of candidate evidence",
};

const STEPS: [string, string][] = [
  ["Read the PDF", "Text with page, paragraph and sentence positions (and review line numbers when the PDF has them), sections, tables with parsed cells, figure captions, the paper checklist and any limitations section."],
  ["Find the claims", "A model lists the paper's own claims from the abstract, introduction and conclusion. Every quote is matched against the PDF text by code; a quote that is not in the paper is dropped and counted."],
  ["Link the evidence", "Explicit references (\"Table 2\", \"Fig. 3\") are resolved first. Then retrieval proposes passages, tables and captions, and a model picks among those candidates only. An unresolved reference is recorded as a dangling reference."],
  ["Check", "Code recomputes numbers from table cells, tests whether 'outperforms' holds in every cell, counts seeds, datasets and baselines against the claim's wording, audits checklist answers against the paper, and compares strong claims with the paper's own limitations."],
  ["Decide", "A fixed precedence table turns check results into one verdict. A judge model reviews every flagged claim; it can confirm a flag, or remove it only by citing evidence that answers it. It can never add or raise a flag."],
  ["Report", "Each claim with its location, evidence, checks, verdict, reason and caveat, plus a coverage ledger of what was and was not checked. Batch reports run three times; a claim's stability is how many runs agreed."],
];

export default function Method() {
  const meta = useMeta();
  return (
    <div className="max-w-3xl space-y-10">
      <PageTitle sub="How a report is made, which models are used, and what Onus does not do.">Method</PageTitle>

      <section>
        <h2 className="text-lg font-semibold">Pipeline</h2>
        <ol className="mt-3 space-y-4">
          {STEPS.map(([t, d], i) => (
            <li key={t} className="grid grid-cols-[1.75rem_1fr] gap-2">
              <span className="tabular-nums text-slate-500">{i + 1}.</span>
              <span>
                <span className="font-medium">{t}. </span>
                <span className="text-slate-700">{d}</span>
              </span>
            </li>
          ))}
        </ol>
        <p className="mt-4 text-slate-700">
          The principle throughout: models point, code computes. Arithmetic, counts, comparisons and lookups are done
          in code, and every quote, evidence id and cited line a model returns is verified before it is used.
        </p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Verdicts</h2>
        <dl className="mt-3 divide-y divide-slate-100">
          {VERDICT_ORDER.map((v) => (
            <div key={v} className="grid gap-1 py-2 sm:grid-cols-[10rem_1fr]">
              <dt>
                <VerdictBadge verdict={v} />
              </dt>
              <dd className="text-slate-700">{VERDICTS[v].description}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-3 text-slate-700">When in doubt, Onus gives the less severe verdict. There is no overall paper score.</p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Models and version</h2>
        <p className="mt-2 text-slate-700">
          Current version: <VersionBadge version={meta.data?.onus_version} fingerprint={meta.data?.fingerprint} />. The
          fingerprint covers the version, the model settings, every prompt, every rubric and the pipeline code: the
          same paper and fingerprint give the same report.
        </p>
        <table className="mt-3 w-full text-left text-sm">
          <thead className="border-b border-slate-300 text-slate-600">
            <tr>
              <th scope="col" className="py-2 pr-3 font-medium">Role</th>
              <th scope="col" className="py-2 pr-3 font-medium">Model</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 align-top">
            {Object.entries(meta.data?.models ?? {}).map(([role, model]) => (
              <tr key={role}>
                <th scope="row" className="py-2 pr-3 font-normal">
                  <span className="font-medium">{role.replace("_", " ")}</span>
                  {ROLES[role] && <span className="block text-xs text-slate-500">{ROLES[role]}</span>}
                </th>
                <td className="py-2 pr-3 font-mono text-xs break-anywhere">{model}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Data</h2>
        <p className="mt-2 text-slate-700">
          The corpus is the Agents4Science 2025 submissions (papers written with AI), split before building into a
          development set and held-out sets. Reviews, scores and decisions are used only to evaluate Onus and to label
          the corpus table; they are never inputs to an analysis. Live analyses read only the PDF.
        </p>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Limits</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-slate-700">
          <li>Values plotted in figures are not read; figures are matched by their captions.</li>
          <li>Tables that cannot be parsed are listed in the coverage ledger; claims that depend on them are not checkable.</li>
          <li>Live analyses run once and without code checks{meta.data ? `, and are limited to ${meta.data.live.max_per_ip_per_hour} per hour` : ""}.</li>
          <li>A flag means the paper does not show the evidence for its wording. It is not a finding of error or misconduct.</li>
        </ul>
      </section>

      <section>
        <h2 className="text-lg font-semibold">Privacy</h2>
        <p className="mt-2 text-slate-700">
          All model reasoning runs on Berget AI in the EU, which does not train on inputs. Uploaded PDFs are stored on
          the Onus server to make repeat analyses instant. Rate limits use a salted hash of your IP address, never the
          address itself. Disputes store only what you type.
        </p>
      </section>
    </div>
  );
}
