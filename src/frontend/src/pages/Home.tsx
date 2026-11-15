import { useMutation, useQuery } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Findings, PaperRow, StartResponse } from "../api/types";
import { useMeta } from "../components/Layout";
import { ErrorBox } from "../components/States";
import { VerdictCounts, VerdictMixBar } from "../components/Verdict";
import { pct } from "../lib/format";

type Source = "arxiv" | "openreview";

function headline(f: Findings): string | null {
  const r2 = f.rq2;
  if (r2 && r2.difference != null && r2.ci95) {
    const pp = (x: number) => `${(x * 100).toFixed(1)} pp`;
    return (
      `Accepted and rejected papers differ in the share of flagged claims by ${pp(r2.difference)} ` +
      `(95% CI ${pp(r2.ci95[0])} to ${pp(r2.ci95[1])}; ${r2.n_accepted} accepted, ${r2.n_rejected} rejected).`
    );
  }
  const r1 = f.rq1?.ai_score;
  if (r1 && r1.rho != null) {
    return `Across ${r1.n} papers, the share of flagged claims and the mean AI reviewer score have Spearman ρ = ${r1.rho.toFixed(2)}.`;
  }
  return null;
}

function StartForm() {
  const meta = useMeta();
  const navigate = useNavigate();
  const [source, setSource] = useState<Source>("arxiv");
  const [ident, setIdent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const ids = { input: useId(), source: useId(), file: useId(), help: useId() };

  const go = (r: StartResponse) =>
    navigate(r.status === "done" ? `/analyses/${r.analysis_id}` : `/live/${r.analysis_id}`);
  const byRef = useMutation({
    mutationFn: () => {
      const v = ident.trim();
      return api.startRef(/^https?:\/\//i.test(v) || v.includes(":") ? { ref: v } : { source, id: v });
    },
    onSuccess: go,
  });
  const byUpload = useMutation({ mutationFn: (f: File) => api.startUpload(f), onSuccess: go });
  const busy = byRef.isPending || byUpload.isPending;
  const live = meta.data?.live;
  const maxMb = live?.max_pdf_mb ?? 20;
  const tooBig = file != null && file.size > maxMb * 1024 * 1024;

  return (
    <section aria-labelledby="start-h" className="rounded border border-slate-200 p-4 sm:p-6">
      <h2 id="start-h" className="text-lg font-semibold">
        Analyse a paper
      </h2>
      <p id={ids.help} className="mt-1 text-sm text-slate-600">
        One run, no code checks; usually a few minutes.
        {live && ` Up to ${live.max_per_ip_per_hour} analyses per hour; PDFs up to ${maxMb} MB.`}
      </p>
      {live && !live.enabled && (
        <p className="mt-3 text-sm text-amber-900">
          Live analysis is switched off on this server. Papers already analysed still open at once.
        </p>
      )}

      <form
        className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (ident.trim()) byRef.mutate();
        }}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor={ids.source} className="text-sm font-medium">
            Source
          </label>
          <select
            id={ids.source}
            value={source}
            onChange={(e) => setSource(e.target.value as Source)}
            className="rounded border border-slate-300 px-2 py-2 text-sm"
          >
            <option value="arxiv">arXiv</option>
            <option value="openreview">OpenReview</option>
          </select>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <label htmlFor={ids.input} className="text-sm font-medium">
            ID or URL
          </label>
          <input
            id={ids.input}
            value={ident}
            onChange={(e) => setIdent(e.target.value)}
            placeholder={source === "arxiv" ? "2401.01234 or https://arxiv.org/abs/…" : "forum id or https://openreview.net/forum?id=…"}
            aria-describedby={ids.help}
            className="w-full min-w-0 rounded border border-slate-300 px-3 py-2 text-sm"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <button
          type="submit"
          disabled={busy || !ident.trim()}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          Analyse
        </button>
      </form>

      <form
        className="mt-5 flex flex-col gap-2 border-t border-slate-100 pt-4 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (file && !tooBig) byUpload.mutate(file);
        }}
      >
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <label htmlFor={ids.file} className="text-sm font-medium">
            Or upload a PDF
          </label>
          <input
            id={ids.file}
            ref={fileRef}
            type="file"
            accept="application/pdf,.pdf"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="w-full min-w-0 text-sm file:mr-3 file:rounded file:border file:border-slate-300 file:bg-white file:px-3 file:py-1.5 file:text-sm"
          />
        </div>
        <button
          type="submit"
          disabled={busy || !file || tooBig}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {byUpload.isPending ? "Uploading…" : "Upload and analyse"}
        </button>
      </form>
      {tooBig && <p className="mt-2 text-sm text-red-800">That file is larger than {maxMb} MB.</p>}
      {byRef.error && <ErrorBox title="Could not start the analysis" error={byRef.error} />}
      {byUpload.error && <ErrorBox title="Could not start the analysis" error={byUpload.error} />}
    </section>
  );
}

function FeaturedRow({ p }: { p: PaperRow }) {
  const s = p.summary;
  return (
    <li className="py-3">
      <Link to={`/papers/${encodeURIComponent(p.ref)}`} className="font-medium text-blue-900 hover:underline break-anywhere">
        {p.title ?? p.ref}
      </Link>
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-slate-600">
        {p.display?.decision && <span className="capitalize">{p.display.decision}</span>}
        <span>
          {pct(s?.flagged_share)} of {s?.n_claims ?? "?"} claims flagged
        </span>
        <VerdictCounts counts={s?.counts} />
      </div>
      <div className="mt-2 max-w-sm">
        <VerdictMixBar counts={s?.counts} />
      </div>
    </li>
  );
}

export default function Home() {
  const meta = useMeta();
  const findings = useQuery({ queryKey: ["findings"], queryFn: api.findings, enabled: !!meta.data?.has_findings });
  const head = findings.data ? headline(findings.data) : null;

  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-3xl font-semibold tracking-tight">A claim map for research papers</h1>
        <p className="mt-3 max-w-prose text-slate-700">
          Onus finds the major claims in a paper, links each one to the table, figure, passage or checklist answer
          behind it, runs checks that code can verify, and gives one verdict per claim. Every verdict links to its
          evidence and can be disputed. There is no overall score, and the report says what was not checked.
        </p>
      </section>

      <StartForm />

      {head && (
        <section aria-labelledby="finding-h">
          <h2 id="finding-h" className="text-lg font-semibold">
            Headline finding
          </h2>
          <p className="mt-2 max-w-prose text-slate-700">{head}</p>
          <Link to="/findings" className="mt-1 inline-block text-sm text-blue-900 underline">
            All findings and what they do not prove
          </Link>
        </section>
      )}

      <section aria-labelledby="featured-h">
        <h2 id="featured-h" className="text-lg font-semibold">
          {meta.data?.featured_source === "config" ? "Featured papers" : "Papers with the most flagged claims"}
        </h2>
        {meta.error && <ErrorBox error={meta.error} />}
        {meta.data && meta.data.featured.length === 0 && (
          <p className="mt-2 text-slate-600">No papers have been analysed yet.</p>
        )}
        <ul className="divide-y divide-slate-200">
          {meta.data?.featured.map((p) => <FeaturedRow key={p.id} p={p} />)}
        </ul>
        {meta.data && meta.data.corpus_papers > 0 && (
          <Link to="/corpus" className="text-sm text-blue-900 underline">
            Browse all {meta.data.corpus_papers} analysed papers
          </Link>
        )}
      </section>
    </div>
  );
}
