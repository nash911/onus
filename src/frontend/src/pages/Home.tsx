/**
 * The landing page (the "Journal" design on the canvas): hero with the start form and a report preview, how it
 * works, the five verdicts, what Onus does not do, examples from /api/meta.featured, and an open-source band.
 */
import { useMutation } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "../api/client";
import type { PaperRow, StartResponse } from "../api/types";
import { OlderVersionBadge, useMeta } from "../components/Layout";
import { CONTRIBUTE_URL, REPO_URL, StarIcon } from "../components/Links";
import { ErrorBox } from "../components/States";
import { VerdictBadge, VerdictCounts } from "../components/Verdict";
import { LIVE_OFF_TEXT, startErrorText } from "../lib/startErrors";
import { track } from "../lib/track";
import { VERDICTS, VERDICT_ORDER } from "../lib/verdicts";

type Source = "arxiv" | "openreview";

const WRAP = "mx-auto w-full max-w-[1120px] px-4 sm:px-6";
const EYEBROW = "font-sans text-xs font-semibold uppercase tracking-[0.08em] text-slate-500";
const H2 = "text-[30px] leading-[1.1] sm:text-[36px]";
const PRIMARY =
  "h-10 shrink-0 rounded-md bg-slate-900 px-[18px] text-sm font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:bg-slate-900";
const FILE =
  "w-full min-w-0 text-sm file:mr-3 file:h-9 file:rounded-md file:border file:border-slate-300 file:bg-white file:px-3.5 file:text-sm file:font-medium file:text-slate-900 hover:file:bg-slate-50";

function StartError({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? startErrorText(error.status, error.message) : startErrorText(-1, String(error));
  return (
    <div className="mt-4 rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900" role="alert">
      <p className="font-medium">{e.title}</p>
      <p className="mt-1 break-anywhere">{e.body}</p>
    </div>
  );
}

function codeProblem(f: File | null, maxMb: number): string | null {
  if (!f) return null;
  if (!/\.zip$/i.test(f.name)) return "The code must be a .zip file.";
  if (f.size > maxMb * 1024 * 1024) return `The code .zip is larger than ${maxMb} MB.`;
  return null;
}

// ------------------------------------------------------------------ start form

function StartForm() {
  const meta = useMeta();
  const navigate = useNavigate();
  const [source, setSource] = useState<Source>("arxiv");
  const [ident, setIdent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  /** One optional code .zip, sent with whichever way the paper is given. */
  const [code, setCode] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const ids = { input: useId(), source: useId(), file: useId(), help: useId(), code: useId(), codeHelp: useId() };

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
      track("submit", { kind: "ref", source: "ref" in body ? "url" : source, ref: v, has_code: codeOn && !!code });
      return api.startRef(body, codeOn ? code : null);
    },
    onSuccess: go,
    onError: failed,
  });
  const byUpload = useMutation({
    mutationFn: (f: File) => {
      track("submit", { kind: "upload", source: "upload", has_code: codeOn && !!code, pdf_kb: Math.round(f.size / 1024) });
      return api.startUpload(f, codeOn ? code : null);
    },
    onSuccess: go,
    onError: failed,
  });
  const busy = byRef.isPending || byUpload.isPending;
  const live = meta.data?.live;
  const off = live != null && !live.enabled;
  const codeOn = !!live?.code_checks && !!live.code;
  const codeMb = live?.code?.max_zip_mb ?? 25;
  const hosts = live?.code?.hosts ?? [];
  const maxMb = live?.max_pdf_mb ?? 20;
  const tooBig = file != null && file.size > maxMb * 1024 * 1024;
  const problem = codeOn ? codeProblem(code, codeMb) : null;

  return (
    <section aria-labelledby="start-h" className="overflow-hidden rounded-[10px] border border-slate-200 bg-white">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-slate-200 px-4 py-3 sm:px-5">
        <h2 id="start-h" className="text-[22px] leading-tight">
          Analyse a paper
        </h2>
        {live?.enabled && (
          <span className="text-[13px] text-slate-600">
            Up to {live.max_per_ip_per_hour} per hour · PDFs up to {maxMb} MB
          </span>
        )}
      </div>
      <div className="px-4 pb-5 pt-4 sm:px-5">
        <p id={ids.help} className="text-sm text-slate-600">
          {codeOn
            ? `One run; usually a few minutes. Code checks run after the report is ready, on the code you add or the ${
                hosts.length ? hosts.join(", ") : "code"
              } link in the paper.`
            : "One run, no code checks; usually a few minutes."}
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
              if (ident.trim() && !problem) byRef.mutate();
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
                className="h-10 rounded-md border border-slate-300 bg-white px-2 text-sm"
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
                className="h-10 w-full min-w-0 rounded-md border border-slate-300 bg-white px-3 text-sm placeholder:text-slate-500"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            <button type="submit" disabled={busy || !ident.trim() || !!problem} className={PRIMARY}>
              {byRef.isPending ? "Starting…" : "Analyse"}
            </button>
          </form>

          <form
            className="mt-4 flex flex-col gap-2 border-t border-slate-200 pt-4 sm:flex-row sm:items-end"
            onSubmit={(e) => {
              e.preventDefault();
              if (file && !tooBig && !problem) byUpload.mutate(file);
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
                className={FILE}
              />
            </div>
            <button type="submit" disabled={busy || !file || tooBig || !!problem} className={PRIMARY}>
              {byUpload.isPending ? "Uploading…" : "Upload and analyse"}
            </button>
          </form>

          {codeOn && (
            <div className="mt-4 flex flex-col gap-1 border-t border-slate-200 pt-4">
              <label htmlFor={ids.code} className="text-sm font-medium">
                Code (optional)
              </label>
              <input
                id={ids.code}
                type="file"
                accept=".zip,application/zip,application/x-zip-compressed"
                aria-describedby={ids.codeHelp}
                onChange={(e) => setCode(e.target.files?.[0] ?? null)}
                className={`${FILE} sm:max-w-md`}
              />
              <span id={ids.codeHelp} className="text-[13px] text-slate-600">
                A .zip up to {codeMb} MB. Leave empty to use the repository linked in the paper.
              </span>
            </div>
          )}
        </fieldset>
        {tooBig && <p className="mt-2 text-sm text-red-800">That file is larger than {maxMb} MB.</p>}
        {problem && (
          <p className="mt-2 text-sm text-red-800" role="alert">
            {problem}
          </p>
        )}
        {byRef.error && <StartError error={byRef.error} />}
        {byUpload.error && <StartError error={byUpload.error} />}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ report preview

/*
 * A real report, copied by hand so the landing page needs no API call. Every string below is verbatim from the
 * production report of paper EJ8cQFi5cU made with pipeline v1.0.0 (fingerprint 9ad544b97609): GET
 * /api/analyses/6aeba484-0f55-4998-8a84-2a817b92d613, claim id 8ed76085b5bf, evidence ev12. passageBefore /
 * passageAfter are the paper's own abstract text either side of the claim's offsets 309-527. If the example
 * changes, copy the values again from the production report; do not edit the wording.
 */
const PREVIEW = {
  title: "AI Passes Humanity’s Last Exam and Generates Video Tutorials", // analysis.title
  claims: 23, // claims.length
  verdict: "supported" as const, // claims[].verdict
  heading: "Abstract", // claims[].location.section
  passageBefore:
    "We demonstrate that AI passes Humanity’s Last Exam by Best-of-N rejection sampling and using appropriate models for different question categories. ",
  quote:
    "Specifically, we pass the HLE with 53% accuracy, without online search, for a cost of around $3 per question and running time of less than 5 minutes per question, verified by humans on a random sample of 100 questions.", // claims[].quote
  passageAfter:
    " We compare the answers and performance of different models and methods and analyze their similarities and differences, finding which pairs of models give the same wrong answers.",
  page: 1, // claims[].location.page
  // claims[].type "result", is_main true, cite(location) and location.section, as the report's claim panel prints them
  meta: "Result claim · main claim · p. 1, ¶ 2, s. 2 · Abstract",
  reason:
    "Supported by p. 3, ¶ 5 (3 Results), Table 1, Table 4 and 1 more. Recomputed: 53% = 53 (Table 1, row 'Anthropic Sonnet 3.7 (for CS/AI) BoN (N=8) high', col 'Accuracy (%)').", // claims[].reason
  stability: { agree: 3, runs: 3 }, // claims[].stability
  evidence: {
    // claims[].evidence[0]
    id: "ev12",
    label: "p. 3, ¶ 5 (3 Results)",
    relation: "supports",
    snippet:
      "We achieve an overall accuracy of 53% on the HLE without using online search, with an average cost of approximately $3 per question and a running time of under 5 minutes per question.",
  },
};

/** A real report in miniature: the claim highlighted in its passage, beside its verdict, reason and one evidence item. */
function ReportPreview() {
  const p = PREVIEW;
  return (
    <figure className="m-0 flex min-w-0 flex-col gap-2.5">
      <div
        className="overflow-hidden rounded-[10px] border border-slate-200 bg-white"
        style={{ boxShadow: "0 1px 2px rgba(27,35,51,0.04), 0 12px 32px -12px rgba(27,35,51,0.18)" }}
      >
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-2.5">
          <span className="truncate font-serif text-[15px] font-semibold">{p.title}</span>
          <span className="shrink-0 text-xs text-slate-600">{p.claims} claims</span>
        </div>
        <div className="flex flex-wrap">
          <div className="min-w-0 flex-[1_1_240px] bg-slate-100 p-4">
            <div className="rounded-sm bg-white px-[18px] pb-[18px] pt-3 font-serif text-[11px] leading-[1.55] shadow-sm">
              <p className="mb-1.5 text-right font-sans text-[10px] text-slate-500">p. {p.page}</p>
              <p className="mb-1.5 text-center font-semibold">{p.heading}</p>
              <p>
                {p.passageBefore}
                <mark
                  className="rounded-sm box-decoration-clone px-px text-inherit"
                  style={{ background: "rgb(6 118 71 / 0.18)", boxShadow: "inset 0 -2px 0 #067647" }}
                >
                  {p.quote}
                </mark>
                {p.passageAfter}
              </p>
              <div className="mt-3 flex flex-col gap-[5px]" aria-hidden="true">
                {["100%", "92%", "96%"].map((w) => (
                  <div key={w} className="h-[5px] rounded-sm bg-slate-200" style={{ width: w }} />
                ))}
              </div>
            </div>
          </div>
          <div className="flex min-w-0 flex-[1_1_220px] flex-col gap-2.5 p-4">
            <span className="self-start">
              <VerdictBadge verdict={p.verdict} />
            </span>
            <p className="text-xs text-slate-600">{p.meta}</p>
            <p className="text-[13px] leading-normal break-anywhere">{p.reason}</p>
            <p className="text-xs text-slate-600">
              Stability: {p.stability.agree}/{p.stability.runs} runs agree
            </p>
            <div className="rounded-md border-2 border-dashed border-slate-900 px-2.5 py-2 text-xs leading-snug">
              <p>
                <span className="font-mono text-slate-500">{p.evidence.id}</span>{" "}
                <span className="font-semibold">{p.evidence.label}</span> {p.evidence.relation}
              </p>
              <p className="mt-1 text-slate-700">{p.evidence.snippet}</p>
            </div>
          </div>
        </div>
      </div>
      <figcaption className="text-[13px] text-slate-600">
        One claim from an example report, highlighted where it appears in the paper, with its verdict, the reason, the
        number recomputed in code and one of the evidence items behind it.
      </figcaption>
    </figure>
  );
}

// ------------------------------------------------------------------ sections

const STEPS = [
  {
    title: "Find the claims",
    body: "A language model reads the paper and lists what it claims. Every quote it returns is checked against the paper's text before it is used.",
  },
  {
    title: "Link the evidence",
    body: "Each claim is tied to the passage, table, figure or code behind it, and you can open that place in the PDF with one click.",
  },
  {
    title: "Decide, with the numbers checked",
    body: "Counts, comparisons and number rechecks are done in code, not by the model. Example reports were run three times and show how many runs agreed.",
  },
];

const LIMITS = [
  { title: "No overall score", body: "Each claim stands on its own evidence. Onus does not grade the paper." },
  {
    title: "Flags are gaps, not misconduct",
    body: "A flag means the evidence in the paper is missing or weaker than the wording, nothing more.",
  },
  {
    title: "It says what it skipped",
    body: "Every report lists the checks that did not run and the claims it could not check, with the reason.",
  },
  {
    title: "Code runs sealed off",
    body: "When a paper's code is checked, linked or uploaded, it is re-run in an isolated sandbox with no access to Onus's keys.",
  },
];

function SectionHead({ id, eyebrow, title, children }: { id: string; eyebrow: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <p className={EYEBROW}>{eyebrow}</p>
      <h2 id={id} className={H2}>
        {title}
      </h2>
      {children}
    </div>
  );
}

function ExampleCard({ p }: { p: PaperRow & { analysis_id: string } }) {
  return (
    <li className="flex min-w-0 flex-col gap-3 rounded-[10px] border border-slate-200 bg-white p-5 hover:border-slate-300">
      <p className="font-serif text-lg font-semibold leading-snug text-slate-900 break-anywhere line-clamp-3">{p.title ?? p.ref}</p>
      <div className="flex flex-wrap items-center gap-2">
        <VerdictCounts counts={p.summary?.counts} />
        <OlderVersionBadge fingerprint={p.fingerprint} version={p.onus_version} />
      </div>
      <Link
        to={`/analyses/${encodeURIComponent(p.analysis_id)}`}
        onClick={() => track("example_click", { ref: p.ref, where: "examples" })}
        className="mt-auto text-sm font-semibold text-blue-900 hover:underline"
      >
        Open report →<span className="sr-only">: {p.title ?? p.ref}</span>
      </Link>
    </li>
  );
}

export default function Home() {
  const meta = useMeta();
  const examples = (meta.data?.featured ?? []).filter((p): p is PaperRow & { analysis_id: string } => !!p.analysis_id);
  const lead = examples[0];

  return (
    <div>
      <section className={`${WRAP} flex flex-wrap items-start gap-12 pb-16 pt-12 sm:pb-[72px] sm:pt-16`}>
        <div className="flex min-w-0 flex-[3_1_500px] flex-col gap-5">
          <p className={EYEBROW}>Evidence check for research papers</p>
          <h1 className="text-[38px] leading-[1.04] tracking-[-0.015em] sm:text-[56px]">Check a paper's claims against its evidence</h1>
          <p className="max-w-[540px] text-lg leading-relaxed text-slate-600">
            Onus finds the claims in a paper, links each one to the passage, table, figure or code behind it, and gives
            each claim one of five verdicts that you can read against the paper itself.
          </p>
          <div className="mt-2">
            <StartForm />
          </div>
          {lead && (
            // The first curated example (FEATURED_PAPERS order), one click from the hero: no form, no waiting.
            <Link
              to={`/analyses/${encodeURIComponent(lead.analysis_id)}`}
              onClick={() => track("example_click", { ref: lead.ref, where: "hero" })}
              className="flex min-w-0 flex-col gap-1 rounded-[10px] border border-blue-200 bg-blue-50 px-4 py-3 hover:border-blue-300 hover:bg-blue-100/60"
            >
              <span className="text-[13px] font-semibold text-blue-900">No paper to hand? Open a finished report, instantly</span>
              <span className="font-serif text-[17px] font-semibold leading-snug text-slate-900 break-anywhere line-clamp-2">
                {lead.title ?? lead.ref}
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <VerdictCounts counts={lead.summary?.counts} />
              </span>
            </Link>
          )}
          <div className="flex flex-wrap gap-x-6 gap-y-2 text-[15px] font-medium">
            {examples.length > 1 && (
              <a href="#examples" className="text-blue-900 hover:underline">
                More example reports →
              </a>
            )}
            <a href={REPO_URL} onClick={() => track("outbound_click", { target: "github_hero" })} className="text-slate-700 hover:underline">
              Open source on GitHub →
            </a>
          </div>
        </div>
        <div className="min-w-0 flex-[2_1_380px] lg:pt-10">
          <ReportPreview />
        </div>
      </section>

      <section aria-labelledby="how-h" id="how" className="scroll-mt-4 border-t border-slate-200">
        <div className={`${WRAP} flex flex-col gap-7 py-16`}>
          <SectionHead id="how-h" eyebrow="How it works" title="The model points; code computes">
            <a href={REPO_URL} onClick={() => track("outbound_click", { target: "github_how" })} className="text-[15px] font-medium text-blue-900 hover:underline">
              Every step is open: read the pipeline on GitHub →
            </a>
          </SectionHead>
          <ol className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(260px,1fr))]">
            {STEPS.map((s, i) => (
              <li key={s.title} className="flex flex-col gap-2.5 rounded-[10px] border border-slate-200 bg-white p-[22px]">
                <span className="font-serif text-[32px] font-semibold leading-none text-blue-900" aria-hidden="true">
                  {i + 1}
                </span>
                <h3 className="text-[17px] font-semibold">{s.title}</h3>
                <p className="text-[15px] leading-relaxed text-slate-600">{s.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section aria-labelledby="verdicts-h" id="verdicts" className="scroll-mt-4 border-t border-slate-200">
        <div className={`${WRAP} flex flex-col gap-7 py-16`}>
          <SectionHead id="verdicts-h" eyebrow="Five verdicts" title="One verdict per claim, never one score per paper" />
          <ul className="grid gap-5 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
            {VERDICT_ORDER.map((v) => (
              <li key={v} className="flex flex-col items-start gap-2">
                <VerdictBadge verdict={v} />
                <span className="text-[15px] leading-normal text-slate-600">{VERDICTS[v].description}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section aria-labelledby="limits-h" className="border-y border-slate-200 bg-slate-50">
        <div className={`${WRAP} flex flex-col gap-7 py-16`}>
          <SectionHead id="limits-h" eyebrow="What Onus does not do" title="Honest about its limits" />
          <ul className="grid gap-x-8 gap-y-6 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
            {LIMITS.map((l) => (
              <li key={l.title} className="flex flex-col gap-1.5 border-t-2 border-slate-900 pt-3.5">
                <h3 className="text-base font-semibold">{l.title}</h3>
                <p className="text-[15px] leading-relaxed text-slate-600">{l.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      {(examples.length > 0 || meta.error) && (
        <section aria-labelledby="examples-h" id="examples" className="scroll-mt-4">
          <div className={`${WRAP} flex flex-col gap-7 py-16`}>
            <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
              <SectionHead id="examples-h" eyebrow="Try an example" title="Open a finished report" />
              <p className="text-[15px] text-slate-600">No sign-up; these open instantly.</p>
            </div>
            {meta.error && <ErrorBox error={meta.error} />}
            <ul className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(280px,1fr))]">
              {examples.map((p) => (
                <ExampleCard key={p.id} p={p} />
              ))}
            </ul>
          </div>
        </section>
      )}

      <section aria-labelledby="oss-h" className="bg-slate-900 text-paper">
        <div className={`${WRAP} flex flex-wrap items-center justify-between gap-x-12 gap-y-7 py-16`}>
          <div className="flex min-w-0 flex-[1_1_420px] flex-col gap-2.5">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-300">Open source</p>
            <h2 id="oss-h" className={H2}>
              Built in the open. Help make it better.
            </h2>
            <p className="text-base leading-relaxed text-slate-200">
              Read how every verdict is reached, run Onus on your own machine, or contribute a check, a fix or an idea.
            </p>
          </div>
          <div className="flex flex-wrap gap-2.5">
            <a
              href={REPO_URL}
              onClick={() => track("outbound_click", { target: "github_band" })}
              className="inline-flex h-11 items-center gap-2 rounded-md bg-paper px-5 text-[15px] font-semibold text-slate-900 hover:bg-white"
            >
              <StarIcon className="h-4 w-4" />
              Star on GitHub
            </a>
            <a
              href={CONTRIBUTE_URL}
              onClick={() => track("outbound_click", { target: "github_contribute" })}
              className="inline-flex h-11 items-center rounded-md border border-slate-400 px-5 text-[15px] font-semibold text-paper hover:bg-slate-800"
            >
              Contribute
            </a>
          </div>
        </div>
      </section>
    </div>
  );
}
