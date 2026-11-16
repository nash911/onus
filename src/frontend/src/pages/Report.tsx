import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { AnalysisResponse, Summary } from "../api/types";
import { ClaimPanel } from "../components/ClaimPanel";
import { Drawer } from "../components/Drawer";
import { IconDownload, IconExternal } from "../components/Icons";
import { OlderVersionBadge, VersionBadge, useMeta } from "../components/Layout";
import { EmptyState, ErrorBox, Loading } from "../components/States";
import { TabPanel, Tabs } from "../components/Tabs";
import { AuditBadge, VerdictBadge } from "../components/Verdict";
import { factLabel, rerunView, type RerunView } from "../lib/codecheck";
import { CHECK_LABELS, cite, date, humanize, pct } from "../lib/format";
import { isOlderVersion } from "../lib/versions";
import { VERDICTS, VERDICT_ORDER } from "../lib/verdicts";
import type { Claim, RunResult, Verdict } from "../types/result";

export function PaperReport() {
  const { ref = "" } = useParams();
  const paper = useQuery({ queryKey: ["paper", ref], queryFn: () => api.paper(ref) });
  if (paper.isPending) return <Loading />;
  if (paper.error) return <ErrorBox title="Could not load this paper" error={paper.error} />;
  if (!paper.data.analysis_id) {
    return <EmptyState title={paper.data.title ?? ref}>This paper has no finished analysis yet.</EmptyState>;
  }
  return <Report id={paper.data.analysis_id} />;
}

export function AnalysisReport() {
  const { id = "" } = useParams();
  return <Report id={id} />;
}

function Report({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["analysis", id], queryFn: () => api.analysis(id), staleTime: Infinity });
  if (q.isPending) return <Loading label="Loading the report…" />;
  if (q.error) {
    return (
      <>
        <ErrorBox title="Could not load this report" error={q.error} />
        <Link to={`/live/${id}`} className="text-sm text-blue-900 underline">
          See the analysis status
        </Link>
      </>
    );
  }
  return <ReportBody r={q.data} />;
}

const TABS = ["claims", "checklist", "code", "coverage", "json"] as const;
type Tab = (typeof TABS)[number];

function ReportBody({ r }: { r: AnalysisResponse }) {
  const meta = useMeta();
  const [params, setParams] = useSearchParams();
  const tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "claims";
  const claimId = params.get("claim");
  const claims = r.claims ?? [];
  const open = claims.find((c) => c.id === claimId) ?? null;
  const summary = (r.summary ?? {}) as Summary;
  const counts = summary.counts ?? {};
  const title = r.display?.title ?? r.analysis.title ?? (r.paper.title as string | undefined) ?? r.analysis.paper_ref;
  const current = meta.data?.fingerprint;

  const setParam = (k: string, v: string | null) =>
    setParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (v == null) n.delete(k);
        else n.set(k, v);
        return n;
      },
      { replace: k === "tab" },
    );

  return (
    <div>
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight break-anywhere">{title}</h1>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-slate-600">
          {r.analysis.source_url && (
            <a href={r.analysis.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-blue-900 underline">
              Source <IconExternal />
              <span className="sr-only">(opens in a new tab)</span>
            </a>
          )}
          <VersionBadge version={r.onus_version} fingerprint={r.fingerprint} label="report" />
          <OlderVersionBadge fingerprint={r.fingerprint} version={r.onus_version} />
          <span>
            {r.mode === "live" ? "Live analysis, 1 run, no code checks" : `Batch analysis, ${r.runs} run${r.runs === 1 ? "" : "s"}`}
            {" · "}
            {date(r.created_at)}
          </span>
          {r.display?.decision && <span className="capitalize">Decision: {r.display.decision}</span>}
          {r.display?.tier && <span>Code tier: {r.display.tier}</span>}
        </div>
        {isOlderVersion(r.fingerprint, current) && (
          <p className="mt-2 text-xs text-slate-500">
            This report was made with v{r.onus_version} (pipeline {r.fingerprint}); this site now runs
            {meta.data ? ` v${meta.data.onus_version}` : ""} (pipeline {current}). Its verdicts may differ from a
            new analysis.
          </p>
        )}
        <ul className="mt-4 flex flex-wrap gap-2" aria-label="Verdict counts">
          {VERDICT_ORDER.map((v) => (
            <li key={v} className="inline-flex items-center gap-1.5">
              <VerdictBadge verdict={v} />
              <span className="tabular-nums text-sm">{counts[v] ?? 0}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-sm text-slate-600">
          {pct(summary.flagged_share)} of {summary.n_claims ?? claims.length} claims flagged; {pct(summary.main_flagged_share)} of main
          claims. Flags mark evidence gaps in the paper, not misconduct.
        </p>
      </header>

      <Tabs
        idPrefix="report"
        active={tab}
        onChange={(t) => setParam("tab", t === "claims" ? null : t)}
        tabs={[
          { id: "claims", label: "Claims", count: claims.length },
          { id: "checklist", label: "Checklist", count: (r.checklist ?? []).length },
          { id: "code", label: "Code" },
          { id: "coverage", label: "Coverage ledger" },
          { id: "json", label: "Download JSON" },
        ]}
      />
      <TabPanel idPrefix="report" id="claims" active={tab}>
        <ClaimsTab claims={claims} onOpen={(c) => setParam("claim", c.id)} />
      </TabPanel>
      <TabPanel idPrefix="report" id="checklist" active={tab}>
        <ChecklistTab r={r} />
      </TabPanel>
      <TabPanel idPrefix="report" id="code" active={tab}>
        <CodeTab r={r} />
      </TabPanel>
      <TabPanel idPrefix="report" id="coverage" active={tab}>
        <CoverageTab r={r} />
      </TabPanel>
      <TabPanel idPrefix="report" id="json" active={tab}>
        <JsonTab r={r} />
      </TabPanel>

      <Drawer open={open != null} onClose={() => setParam("claim", null)} title="Claim">
        {open && <ClaimPanel claim={open} analysisId={r.analysis.id} />}
      </Drawer>
    </div>
  );
}

// ---------------------------------------------------------------- claims

function ClaimsTab({ claims, onOpen }: { claims: Claim[]; onOpen: (c: Claim) => void }) {
  const [verdicts, setVerdicts] = useState<Set<Verdict>>(new Set());
  const [type, setType] = useState("");
  const [mainOnly, setMainOnly] = useState(false);
  const [text, setText] = useState("");
  const types = useMemo(() => Array.from(new Set(claims.map((c) => c.type))).sort(), [claims]);
  const shown = useMemo(() => {
    const t = text.trim().toLowerCase();
    return [...claims]
      .filter((c) => (verdicts.size ? verdicts.has(c.verdict) : true))
      .filter((c) => (type ? c.type === type : true))
      .filter((c) => (mainOnly ? c.is_main : true))
      .filter((c) => (t ? c.quote.toLowerCase().includes(t) : true))
      .sort((a, b) => b.risk - a.risk || a.location.start - b.location.start);
  }, [claims, verdicts, type, mainOnly, text]);

  const toggle = (v: Verdict) =>
    setVerdicts((s) => {
      const n = new Set(s);
      if (n.has(v)) n.delete(v);
      else n.add(v);
      return n;
    });

  return (
    <div>
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end">
        <fieldset>
          <legend className="text-sm font-medium">Verdict</legend>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {VERDICT_ORDER.map((v) => {
              const { Icon, label, fill } = VERDICTS[v];
              const on = verdicts.has(v);
              return (
                <button
                  key={v}
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggle(v)}
                  className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs ${
                    on ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  <Icon style={{ color: on ? "white" : fill }} />
                  {label}
                </button>
              );
            })}
          </div>
        </fieldset>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Type
          <select value={type} onChange={(e) => setType(e.target.value)} className="rounded border border-slate-300 px-2 py-1 text-sm font-normal">
            <option value="">All types</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {humanize(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium">
          Search quotes
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm font-normal"
            type="search"
          />
        </label>
        <label className="inline-flex items-center gap-2 pb-1 text-sm">
          <input type="checkbox" checked={mainOnly} onChange={(e) => setMainOnly(e.target.checked)} />
          Main claims only
        </label>
      </div>

      <p className="mt-4 text-sm text-slate-600" role="status">
        {shown.length} of {claims.length} claims, highest risk first. Select a claim for its evidence and checks.
      </p>
      <ul className="mt-2 divide-y divide-slate-200 border-y border-slate-200">
        {shown.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              onClick={() => onOpen(c)}
              className="grid w-full grid-cols-1 gap-1 px-1 py-3 text-left hover:bg-slate-50 sm:grid-cols-[9rem_1fr] sm:gap-4"
            >
              <span className="flex flex-wrap items-center gap-2 sm:flex-col sm:items-start">
                <VerdictBadge verdict={c.verdict} />
                {c.stability && (
                  <span className="text-xs text-slate-500">
                    {c.stability.agree}/{c.stability.runs} runs
                  </span>
                )}
              </span>
              <span className="min-w-0">
                <span className="block text-slate-900 break-anywhere">{c.quote}</span>
                <span className="mt-1 block text-xs text-slate-500">
                  {cite(c.location)}
                  {c.location.section ? ` · ${c.location.section}` : ""} · {humanize(c.type)}
                  {c.is_main ? " · main" : ""}
                </span>
                <span className="mt-1 block text-sm text-slate-600 break-anywhere">{c.reason}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- checklist, code, coverage, json

function ChecklistTab({ r }: { r: RunResult }) {
  const items = r.checklist ?? [];
  if (!items.length) return <EmptyState title="No paper checklist was found in this PDF." />;
  return (
    <div>
      <p className="mb-3 max-w-prose text-sm text-slate-600">
        Each answer in the paper's checklist, and whether the paper itself backs it. "No" and "NA" answers are not
        applicable.
      </p>
      <ul className="divide-y divide-slate-200 border-y border-slate-200">
        {items.map((a) => (
          <li key={a.key} className="py-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{a.title}</span>
              <span className="text-sm text-slate-600">Answer: {a.answer}</span>
              <AuditBadge status={a.status} />
            </div>
            <p className="mt-1 text-sm text-slate-700 break-anywhere">{a.reason}</p>
            {a.justification && (
              <p className="mt-1 text-xs text-slate-500 break-anywhere">Authors' justification: {a.justification}</p>
            )}
            {(a.evidence ?? []).length > 0 && (
              <details className="mt-1 text-sm">
                <summary className="cursor-pointer text-blue-900">Evidence ({(a.evidence ?? []).length})</summary>
                <ul className="mt-1 space-y-2">
                  {(a.evidence ?? []).map((e) => (
                    <li key={e.id} className="border-l-2 border-slate-200 pl-3">
                      <span className="font-medium">{e.label}</span>
                      {e.location && <span className="text-xs text-slate-500"> · {cite(e.location)}</span>}
                      <p className="text-slate-700 break-anywhere">{e.snippet}</p>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function CodeTab({ r }: { r: RunResult }) {
  const code = r.code;
  const statics = code.static ?? [];
  const reruns = code.reruns ?? [];
  return (
    <div className="max-w-prose space-y-3 text-sm text-slate-700">
      <p>
        {code.available ? `Submitted code: ${code.files} files.` : "No code was submitted with this paper."}
        {code.code_version ? ` Code checks version ${code.code_version}.` : ""}
      </p>
      {r.mode === "live" && <p>Live analysis does not run code checks.</p>}
      {!statics.length && !reruns.length && code.available && r.mode !== "live" && (
        <p>Code checks are not part of this report's version. The coverage ledger lists this.</p>
      )}
      {statics.length > 0 && (
        <section>
          <h3 className="font-semibold text-slate-900">Paper–code facts</h3>
          <ul className="mt-1 divide-y divide-slate-100">
            {statics.map((f, i) => (
              <li key={i} className="py-2">
                <span className="font-medium">{factLabel(f.status)}:</span>{" "}
                <span className="break-anywhere">{String(f.fact ?? f.statement ?? "")}</span>
                {Array.isArray(f.citations) && f.citations.length > 0 && (
                  <span className="block font-mono text-xs text-slate-500 break-anywhere">{f.citations.join(", ")}</span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {reruns.length > 0 && (
        <section>
          <h3 className="font-semibold text-slate-900">Re-runs</h3>
          <ul className="mt-1 divide-y divide-slate-100">
            {reruns.map((x, i) => (
              <RerunItem key={i} v={rerunView(x as Record<string, unknown>)} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

const TONE: Record<RerunView["tone"], string> = {
  good: "border-emerald-300 bg-emerald-50 text-emerald-900",
  bad: "border-red-300 bg-red-50 text-red-900",
  caution: "border-amber-300 bg-amber-50 text-amber-900",
  neutral: "border-slate-300 bg-slate-50 text-slate-800",
};

function RerunItem({ v }: { v: RerunView }) {
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`inline-flex rounded border px-1.5 py-0.5 text-xs font-medium ${TONE[v.tone]}`}>{v.label}</span>
        {!v.known && <span className="text-xs text-slate-500">(status as recorded)</span>}
        {v.minutes != null && <span className="text-xs text-slate-500">{v.minutes.toFixed(1)} min</span>}
      </div>
      {v.explain && <p className="mt-1">{v.explain}</p>}
      {v.errorLine && (
        <p className="mt-1">
          Error: <code className="font-mono text-xs break-anywhere">{v.errorLine}</code>
        </p>
      )}
      {v.reason && !v.errorLine && <p className="mt-1 text-xs text-slate-500 break-anywhere">{v.reason}</p>}
      {v.modifications.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-blue-900">
            Changes made to the code ({v.modifications.length} file{v.modifications.length === 1 ? "" : "s"})
          </summary>
          <ul className="mt-1 space-y-2">
            {v.modifications.map((m, j) => (
              <li key={j}>
                <p className="font-mono text-xs break-anywhere">{m.path}</p>
                {m.change && <p className="text-xs text-slate-600 break-anywhere">{m.change}</p>}
                {m.diff && (
                  <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs break-anywhere">
                    {m.diff}
                  </pre>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      {v.commands.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-blue-900">Commands ({v.commands.length})</summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs break-anywhere">
            {v.commands.join("\n")}
          </pre>
        </details>
      )}
    </li>
  );
}

function CoverageTab({ r }: { r: RunResult }) {
  const ledger = r.ledger ?? [];
  const notChecked = r.not_checked ?? [];
  const cost = (r.cost ?? {}) as { eur?: number; calls?: number };
  const timings = (r.timings ?? {}) as { wall_s?: number };
  return (
    <div className="space-y-8">
      <section>
        <h3 className="font-semibold">Which checks ran</h3>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-0 text-left text-sm">
            <thead className="border-b border-slate-300 text-slate-600">
              <tr>
                <th scope="col" className="py-2 pr-3 font-medium">Check</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Ran on</th>
                <th scope="col" className="py-2 pr-3 text-right font-medium">Not run</th>
                <th scope="col" className="hidden py-2 font-medium sm:table-cell">Why not</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 align-top">
              {ledger.map((l) => (
                <tr key={l.check}>
                  <th scope="row" className="py-2 pr-3 font-normal">
                    {CHECK_LABELS[l.check] ?? l.check}
                    <ul className="mt-1 text-xs text-slate-500 sm:hidden">
                      {Object.entries(l.reasons ?? {}).map(([k, n]) => (
                        <li key={k}>{k}: {n}</li>
                      ))}
                    </ul>
                  </th>
                  <td className="py-2 pr-3 text-right tabular-nums">{l.ran_on}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">{l.not_run_on}</td>
                  <td className="hidden py-2 text-slate-600 sm:table-cell">
                    <ul>
                      {Object.entries(l.reasons ?? {}).map(([k, n]) => (
                        <li key={k}>
                          {k}: <span className="tabular-nums">{n}</span>
                        </li>
                      ))}
                    </ul>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section>
        <h3 className="font-semibold">What was not checked</h3>
        {notChecked.length ? (
          <ul className="mt-2 divide-y divide-slate-100 text-sm">
            {notChecked.map((n, i) => (
              <li key={i} className="py-2">
                <span className="font-medium">{n.what}</span>
                {(n.count ?? 1) > 1 && <span className="tabular-nums text-slate-500"> ({n.count})</span>}
                <span className="text-slate-600">: {n.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-slate-600">Nothing recorded.</p>
        )}
      </section>
      {(r.warnings ?? []).length > 0 && (
        <section>
          <h3 className="font-semibold">Warnings</h3>
          <ul className="mt-2 list-disc pl-5 text-sm text-slate-700">
            {(r.warnings ?? []).map((w, i) => (
              <li key={i} className="break-anywhere">{w}</li>
            ))}
          </ul>
        </section>
      )}
      <section className="text-sm text-slate-600">
        <h3 className="font-semibold text-slate-900">Run</h3>
        <p className="mt-1 break-anywhere">
          Models: {Object.entries(r.models).map(([k, v]) => `${k} ${v}`).join(" · ")}
        </p>
        <p className="mt-1">
          {cost.eur != null && `Model cost €${cost.eur.toFixed(3)}`}
          {cost.calls != null && ` · ${cost.calls} calls`}
          {timings.wall_s != null && ` · ${Math.round(timings.wall_s)} s`}
        </p>
      </section>
    </div>
  );
}

function JsonTab({ r }: { r: AnalysisResponse }) {
  const { analysis, display: _display, ...result } = r;
  void _display;
  const download = () => {
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `onus_${analysis.paper_ref.replace(/[^A-Za-z0-9._-]+/g, "_")}_${r.fingerprint}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <div className="max-w-prose space-y-3 text-sm text-slate-700">
      <p>
        The full result in the published schema (schema version {r.schema_version ?? "1"}): every claim, its
        evidence, checks, verdict and reason, the checklist audit and the coverage ledger.
      </p>
      <button
        type="button"
        onClick={download}
        className="inline-flex items-center gap-2 rounded bg-slate-900 px-4 py-2 font-medium text-white"
      >
        <IconDownload /> Download JSON
      </button>
      <p>
        Or fetch it directly:{" "}
        <a className="font-mono text-blue-900 underline break-anywhere" href={`/api/analyses/${analysis.id}`}>
          /api/analyses/{analysis.id}
        </a>
      </p>
    </div>
  );
}
