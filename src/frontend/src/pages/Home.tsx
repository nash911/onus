import { useMutation } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { PaperRow, StartResponse } from "../api/types";
import { OlderVersionBadge, useMeta } from "../components/Layout";
import { ErrorBox } from "../components/States";
import { VerdictCounts } from "../components/Verdict";
import { LIVE_OFF_TEXT, startErrorText } from "../lib/startErrors";
import { track } from "../lib/track";
import { VERDICTS, VERDICT_ORDER } from "../lib/verdicts";

type Source = "arxiv" | "openreview";


function StartError({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? startErrorText(error.status, error.message) : startErrorText(-1, String(error));
  return (
    <div className="mt-4 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900" role="alert">
      <p className="font-medium">{e.title}</p>
      <p className="mt-1 break-anywhere">{e.body}</p>
    </div>
  );
}

/** Optional code .zip next to a paper input (only when live code checks are on). */
function CodeInput({ id, maxMb, onChange }: { id: string; maxMb: number; onChange: (f: File | null) => void }) {
  const helpId = `${id}-help`;
  return (
    <div className="flex min-w-0 flex-col gap-1 sm:max-w-xs">
      <label htmlFor={id} className="text-sm font-medium">
        Code (optional)
      </label>
      <input
        id={id}
        type="file"
        accept=".zip,application/zip,application/x-zip-compressed"
        aria-describedby={helpId}
        onChange={(e) => onChange(e.target.files?.[0] ?? null)}
        className="w-full min-w-0 text-sm file:mr-3 file:rounded file:border file:border-slate-300 file:bg-white file:px-3 file:py-1.5 file:text-sm"
      />
      <span id={helpId} className="text-xs text-slate-500">
        A .zip up to {maxMb} MB
      </span>
    </div>
  );
}

function codeProblem(f: File | null, maxMb: number): string | null {
  if (!f) return null;
  if (!/\.zip$/i.test(f.name)) return "The code must be a .zip file.";
  if (f.size > maxMb * 1024 * 1024) return `The code .zip is larger than ${maxMb} MB.`;
  return null;
}

function StartForm() {
  const meta = useMeta();
  const navigate = useNavigate();
  const [source, setSource] = useState<Source>("arxiv");
  const [ident, setIdent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [refCode, setRefCode] = useState<File | null>(null);
  const [pdfCode, setPdfCode] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const ids = { input: useId(), source: useId(), file: useId(), help: useId(), refCode: useId(), pdfCode: useId() };

  // the report page shows the progress while the analysis runs
  const go = (r: StartResponse) => {
    track("submit_result", { status: r.status, cached: r.cached });
    navigate(`/analyses/${r.analysis_id}`);
  };
  const failed = (e: Error) => track("submit_result", { status: "error", http: e instanceof ApiError ? e.status : -1 });
  const byRef = useMutation({
    mutationFn: () => {
      const v = ident.trim();
      const body = /^https?:\/\//i.test(v) || v.includes(":") ? { ref: v } : { source, id: v };
      track("submit", { kind: "ref", source: "ref" in body ? "url" : source, ref: v, has_code: codeOn && !!refCode });
      return api.startRef(body, codeOn ? refCode : null);
    },
    onSuccess: go,
    onError: failed,
  });
  const byUpload = useMutation({
    mutationFn: (f: File) => {
      track("submit", { kind: "upload", source: "upload", has_code: codeOn && !!pdfCode, pdf_kb: Math.round(f.size / 1024) });
      return api.startUpload(f, codeOn ? pdfCode : null);
    },
    onSuccess: go,
    onError: failed,
  });
  const busy = byRef.isPending || byUpload.isPending;
  const live = meta.data?.live;
  const off = live != null && !live.enabled;
  const codeOn = !!live?.code_checks && !!live.code;
  const codeMb = live?.code?.max_zip_mb ?? 25;
  const maxMb = live?.max_pdf_mb ?? 20;
  const tooBig = file != null && file.size > maxMb * 1024 * 1024;
  const refCodeProblem = codeOn ? codeProblem(refCode, codeMb) : null;
  const pdfCodeProblem = codeOn ? codeProblem(pdfCode, codeMb) : null;

  return (
    <section aria-labelledby="start-h" className="rounded border border-slate-200 p-4 sm:p-6">
      <h2 id="start-h" className="text-lg font-semibold">
        Analyse a paper
      </h2>
      <p id={ids.help} className="mt-1 text-sm text-slate-600">
        {codeOn
          ? `One run; usually a few minutes. Code checks run after the report is ready, on the code you add or the paper's ${live?.code?.hosts.join(", ") ?? ""} link.`
          : "One run, no code checks; usually a few minutes."}
        {live?.enabled && ` Up to ${live.max_per_ip_per_hour} analyses per hour; PDFs up to ${maxMb} MB.`}
      </p>
      {off && (
        <p className="mt-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900" role="note">
          {LIVE_OFF_TEXT}
        </p>
      )}
      <fieldset disabled={off} className="disabled:opacity-60">
        <legend className="sr-only">{off ? "Analyse a paper (switched off)" : "Analyse a paper"}</legend>

      <form
        className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (ident.trim() && !refCodeProblem) byRef.mutate();
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
        {codeOn && <CodeInput id={ids.refCode} maxMb={codeMb} onChange={setRefCode} />}
        <button
          type="submit"
          disabled={busy || !ident.trim() || !!refCodeProblem}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          Analyse
        </button>
      </form>

      <form
        className="mt-5 flex flex-col gap-2 border-t border-slate-100 pt-4 sm:flex-row sm:items-end"
        onSubmit={(e) => {
          e.preventDefault();
          if (file && !tooBig && !pdfCodeProblem) byUpload.mutate(file);
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
        {codeOn && <CodeInput id={ids.pdfCode} maxMb={codeMb} onChange={setPdfCode} />}
        <button
          type="submit"
          disabled={busy || !file || tooBig || !!pdfCodeProblem}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {byUpload.isPending ? "Uploading…" : "Upload and analyse"}
        </button>
      </form>
      </fieldset>
      {tooBig && <p className="mt-2 text-sm text-red-800">That file is larger than {maxMb} MB.</p>}
      {(refCodeProblem ?? pdfCodeProblem) && (
        <p className="mt-2 text-sm text-red-800" role="alert">
          {refCodeProblem ?? pdfCodeProblem}
        </p>
      )}
      {byRef.error && <StartError error={byRef.error} />}
      {byUpload.error && <StartError error={byUpload.error} />}
    </section>
  );
}

/** One line per verdict, in risk order. Colour is never the only signal: icon and name sit next to it. */
function VerdictRow() {
  return (
    <ul className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2 lg:grid-cols-5" aria-label="The five verdicts">
      {VERDICT_ORDER.map((v) => {
        const { Icon, label, fill, description } = VERDICTS[v];
        return (
          <li key={v} className="min-w-0">
            <span className="inline-flex items-center gap-1 font-medium text-slate-900">
              <Icon style={{ color: fill }} />
              {label}
            </span>
            <span className="block text-slate-600">{description}</span>
          </li>
        );
      })}
    </ul>
  );
}

function ExampleCard({ p }: { p: PaperRow & { analysis_id: string } }) {
  return (
    <li className="flex min-w-0 flex-col rounded border border-slate-200 p-4">
      <p className="font-medium text-slate-900 break-anywhere line-clamp-3">{p.title ?? p.ref}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <VerdictCounts counts={p.summary?.counts} />
        <OlderVersionBadge fingerprint={p.fingerprint} version={p.onus_version} />
      </div>
      <Link
        to={`/analyses/${encodeURIComponent(p.analysis_id)}`}
        onClick={() => track("example_click", { ref: p.ref })}
        className="mt-auto pt-3 text-sm font-medium text-blue-900 underline"
      >
        Open report
        <span className="sr-only">: {p.title ?? p.ref}</span>
      </Link>
    </li>
  );
}

export default function Home() {
  const meta = useMeta();
  const examples = (meta.data?.featured ?? []).filter((p): p is PaperRow & { analysis_id: string } => !!p.analysis_id);

  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-3xl font-semibold tracking-tight">Check a paper's claims against its evidence</h1>
        <p className="mt-3 max-w-prose text-slate-700">
          Onus finds the claims in a paper, links each one to the passage, table, figure or code behind it, and gives
          each claim one of five verdicts that you can read against the paper itself.
        </p>
      </section>

      <VerdictRow />

      <StartForm />

      <details className="max-w-prose rounded border border-slate-200 px-4 py-3 text-sm text-slate-700">
        <summary className="cursor-pointer font-medium text-slate-900">How it works</summary>
        <p className="mt-2">
          The model points and code computes: a language model finds claims and candidate evidence, and every count,
          comparison and number recheck is done in code. Every quote the model returns is verified against the
          paper's text before it is used. Submitted code is re-run in an isolated sandbox with no access to Onus's
          keys. There is no overall score: each claim gets its own verdict with the evidence, and the report says
          what was not checked.
        </p>
      </details>

      {meta.error && <ErrorBox error={meta.error} />}
      {examples.length > 0 && (
        <section aria-labelledby="examples-h">
          <h2 id="examples-h" className="text-lg font-semibold">
            Try an example
          </h2>
          <ul className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {examples.map((p) => (
              <ExampleCard key={p.id} p={p} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
