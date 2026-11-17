/**
 * The report, read against the paper: /analyses/:id.
 *
 * Desktop (>= 1024 px): the PDF on the left (about 58%), the report on the right (about 42%); each pane scrolls on
 * its own. Narrow screens: Paper and Report tabs. While the analysis runs, the report pane shows the progress
 * stages and the PDF appears as soon as /pdf answers; the report replaces the progress without a reload.
 * Selection lives in the URL (?claim=<id>&ev=<ev_id>&tab=...), so a link opens the same view.
 */
import { useQuery } from "@tanstack/react-query";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { AnalysisResponse, AnalysisStatus } from "../api/types";
import { ClaimPanel, isCodeEvidence, type EvidencePlace } from "../components/ClaimPanel";
import { IconExternal, IconSpinner } from "../components/Icons";
import { OlderVersionBadge, VersionBadge, useMeta } from "../components/Layout";
import type { PdfHighlight, PdfMark, ScrollRequest } from "../components/PdfViewer";
import { ProgressStages } from "../components/ProgressStages";
import { ChecklistSection, CodeSection, CodeStageNote, NotCheckedSection } from "../components/ReportSections";
import { ErrorBox, Loading } from "../components/States";
import { TabPanel, Tabs } from "../components/Tabs";
import { VerdictBadge } from "../components/Verdict";
import { EMPTY_ANCHORS, anchorKey, checklistKey, firstRect, type Anchor, type Anchors } from "../lib/anchors";
import { codeStagePending, liveModeLabel } from "../lib/codeStage";
import { date } from "../lib/format";
import {
  ALL_VERDICTS,
  DEFAULT_FILTER,
  exportName,
  filterClaims,
  readSelection,
  reportMarkdown,
  stepClaim,
  toggleVerdict,
  writeSelection,
  type ClaimFilter,
  type ReportTab,
  type Selection,
} from "../lib/reportView";
import { setTrackedAnalysis, track } from "../lib/track";
import { VERDICTS } from "../lib/verdicts";
import type { ChecklistAudit, Claim, Evidence1, Verdict } from "../types/result";

const PdfViewer = lazy(() => import("../components/PdfViewer"));

function useMediaQuery(q: string): boolean {
  const get = () => typeof window !== "undefined" && !!window.matchMedia?.(q).matches;
  const [m, setM] = useState(get);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const on = () => setM(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [q]);
  return m;
}

const isTerminal = (s?: AnalysisStatus["status"]) => s === "done" || s === "failed";

export default function ReportPage() {
  const { id = "" } = useParams();
  const meta = useMeta();
  const status = useQuery({
    queryKey: ["status", id],
    queryFn: () => api.status(id),
    refetchInterval: (q) => (isTerminal(q.state.data?.status) ? false : 1500),
  });
  const done = status.data?.status === "done";
  const report = useQuery({
    queryKey: ["analysis", id],
    queryFn: () => api.analysis(id),
    enabled: done,
    staleTime: Infinity,
    // while the live code checks run, the report polls and updates in place
    refetchInterval: (q) => (codeStagePending(q.state.data?.code_stage) ? 4000 : false),
  });
  const anchors = useQuery({ queryKey: ["anchors", id], queryFn: () => api.anchors(id), enabled: done, staleTime: Infinity });
  const [pdfProblem, setPdfProblem] = useState<"missing" | "error" | null>(null);
  const opened = useRef<string | null>(null);
  useEffect(() => {
    const r = report.data;
    if (!r || opened.current === id) return;
    opened.current = id;
    const flagged = (r.claims ?? []).filter((c) => c.verdict === "overstated" || c.verdict === "untraced" || c.verdict === "contradicted").length;
    track("report_open", { status: "done", mode: r.mode, n_claims: (r.claims ?? []).length, flagged, has_code: !!r.code?.available });
  }, [report.data, id]);
  const wide = useMediaQuery("(min-width: 1024px)");

  // While running, a 404 from /pdf only means "not downloaded yet": try again when the stage moves on.
  const stage = status.data?.progress?.stage ?? status.data?.status ?? "";
  useEffect(() => {
    if (!done) setPdfProblem(null);
  }, [stage, done]);

  // analytics: every event on this page carries the analysis id; the progress screen reports its stages
  useEffect(() => {
    setTrackedAnalysis(id);
    return () => setTrackedAnalysis(undefined);
  }, [id]);
  useEffect(() => {
    if (stage && !done) track("progress_view", { stage });
  }, [stage, done]);

  const a = anchors.data ?? EMPTY_ANCHORS;
  const noPdf = (done && anchors.isSuccess && !a.pdf) || (done && pdfProblem != null);
  const title = report.data ? reportTitle(report.data) : null;
  useEffect(() => {
    document.title = title ? `${title} · Onus` : "Onus";
    return () => {
      document.title = "Onus";
    };
  }, [title]);

  if (status.isPending) return <Centered><Loading label="Loading the report…" /></Centered>;
  if (status.error) return <Centered><ErrorBox title="Could not load this analysis" error={status.error} /></Centered>;

  const s = status.data;
  const pdfPane = noPdf ? null : (
    <Suspense fallback={<Loading label="Loading the paper viewer…" />}>
      <PdfSlot key={done ? "done" : stage} id={id} anchors={a} onProblem={setPdfProblem} problem={pdfProblem} done={done} />
    </Suspense>
  );

  if (!done) {
    return (
      <SplitLayout
        wide={wide}
        pdf={pdfPane}
        report={
          <div className="p-4 sm:p-6">
            <ProgressStages s={s} codeChecks={!!meta.data?.live.code_checks} />
          </div>
        }
        onPaper={false}
        setOnPaper={() => undefined}
      />
    );
  }
  if (report.isPending || anchors.isPending) return <Centered><Loading label="Loading the report…" /></Centered>;
  if (report.error) return <Centered><ErrorBox title="Could not load this report" error={report.error} /></Centered>;
  return <ReportView r={report.data} anchors={a} noPdf={noPdf} wide={wide} onProblem={setPdfProblem} problem={pdfProblem} />;
}

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="mx-auto w-full max-w-3xl px-4 py-8">{children}</div>;
}

function reportTitle(r: AnalysisResponse): string {
  return r.display?.title ?? r.analysis.title ?? (r.paper.title as string | undefined) ?? r.analysis.paper_ref;
}

function authorsOf(r: AnalysisResponse): string | null {
  const raw = (r.paper as Record<string, unknown>).authors ?? (r.display as Record<string, unknown> | null)?.authors;
  if (Array.isArray(raw)) return raw.filter((x) => typeof x === "string").join(", ") || null;
  return typeof raw === "string" && raw.trim() ? raw.trim() : null;
}

// ---------------------------------------------------------------- the PDF pane

interface PdfSlotProps {
  id: string;
  anchors: Anchors;
  done: boolean;
  problem: "missing" | "error" | null;
  onProblem: (p: "missing" | "error") => void;
  marks?: PdfMark[];
  selected?: { verdict: Verdict; highlights: PdfHighlight[] } | null;
  evidence?: PdfHighlight[] | null;
  scroll?: ScrollRequest | null;
  onMark?: (claimId: string) => void;
  active?: boolean;
}

function PdfSlot({ id, anchors, done, problem, onProblem, marks, selected, evidence, scroll, onMark, active }: PdfSlotProps) {
  if (problem && !done) {
    return (
      <p className="flex items-center gap-2 p-6 text-sm text-slate-600" role="status">
        <IconSpinner /> The paper appears here once it has been downloaded.
      </p>
    );
  }
  return (
    <PdfViewer
      url={api.pdfUrl(id)}
      pageSizes={anchors.page_sizes}
      marks={marks ?? []}
      selected={selected ?? null}
      evidence={evidence ?? null}
      scroll={scroll ?? null}
      onMark={onMark ?? (() => undefined)}
      onUnavailable={onProblem}
      active={active}
    />
  );
}

function SplitLayout({ wide, pdf, report, onPaper, setOnPaper, canGoBack }: {
  wide: boolean;
  pdf: React.ReactNode | null;
  report: React.ReactNode;
  onPaper: boolean;
  setOnPaper: (v: boolean) => void;
  canGoBack?: boolean;
}) {
  if (!pdf) {
    return (
      <div className="h-full min-h-0 overflow-y-auto">
        <p className="border-b border-slate-200 bg-slate-50 px-4 py-2 text-sm text-slate-600">Paper preview not available for this report.</p>
        <div className="mx-auto max-w-3xl">{report}</div>
      </div>
    );
  }
  if (wide) {
    return (
      <div className="grid h-full min-h-0 grid-cols-[58fr_42fr]">
        <section aria-label="Paper" className="min-h-0 border-r border-slate-200">{pdf}</section>
        <section aria-label="Report" className="min-h-0 overflow-y-auto" id="report-pane">{report}</section>
      </div>
    );
  }
  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div role="tablist" aria-label="View" className="flex border-b border-slate-200 bg-white">
        {(["paper", "report"] as const).map((t) => {
          const sel = (t === "paper") === onPaper;
          return (
            <button
              key={t}
              role="tab"
              id={`view-tab-${t}`}
              aria-selected={sel}
              aria-controls={`view-panel-${t}`}
              onClick={() => setOnPaper(t === "paper")}
              className={`-mb-px flex-1 border-b-2 px-3 py-2 text-sm ${sel ? "border-slate-900 font-medium" : "border-transparent text-slate-600"}`}
            >
              {t === "paper" ? "Paper" : "Report"}
            </button>
          );
        })}
      </div>
      <div id="view-panel-paper" role="tabpanel" aria-labelledby="view-tab-paper" className={`min-h-0 flex-1 ${onPaper ? "" : "hidden"}`}>
        {pdf}
      </div>
      <div id="view-panel-report" role="tabpanel" aria-labelledby="view-tab-report" className={`min-h-0 flex-1 overflow-y-auto ${onPaper ? "hidden" : ""}`}>
        {report}
      </div>
      {onPaper && canGoBack && (
        <button
          type="button"
          onClick={() => setOnPaper(false)}
          className="absolute bottom-4 left-1/2 z-10 -translate-x-1/2 rounded-full bg-slate-900 px-4 py-2 text-sm font-medium text-white shadow-lg"
        >
          Back to report
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- the report

function ReportView({ r, anchors, noPdf, wide, onProblem, problem }: {
  r: AnalysisResponse;
  anchors: Anchors;
  noPdf: boolean;
  wide: boolean;
  onProblem: (p: "missing" | "error") => void;
  problem: "missing" | "error" | null;
}) {
  const [params, setParams] = useSearchParams();
  const sel = readSelection(params);
  const claims = useMemo(() => r.claims ?? [], [r.claims]);
  const [filter, setFilter] = useState<ClaimFilter>(DEFAULT_FILTER);
  const shown = useMemo(() => filterClaims(claims, filter), [claims, filter]);
  useEffect(() => {
    if (!filter.text.trim()) return;
    const t = window.setTimeout(() => track("search", { length: filter.text.trim().length, results: shown.length }), 1200);
    return () => window.clearTimeout(t);
  }, [filter.text, shown.length]);
  const [expanded, setExpanded] = useState<boolean>(() => !!sel.claim);
  const [onPaper, setOnPaper] = useState(false);
  const [scroll, setScroll] = useState<ScrollRequest | null>(null);
  const seq = useRef(0);
  const items = anchors.items;
  const pdfOn = !noPdf;

  const byChecklistKey = useMemo(() => {
    const m = new Map<string, Claim>();
    for (const c of claims) if (c.checklist_key) m.set(c.checklist_key, c);
    return m;
  }, [claims]);
  const checklistSel = sel.claim?.startsWith("checklist:") ? sel.claim.slice("checklist:".length) : null;
  const claim = checklistSel ? null : (claims.find((c) => c.id === sel.claim) ?? null);

  const update = useCallback(
    (s: Partial<Selection>) => setParams((p) => writeSelection(p, s), { replace: true }),
    [setParams],
  );

  const scrollTo = useCallback((list: Anchor[] | undefined, fallbackPage: number | undefined) => {
    const t = firstRect(list) ?? (fallbackPage ? { page: fallbackPage, rect: null } : null);
    if (t) setScroll({ ...t, seq: ++seq.current });
  }, []);

  // ---- anchors for claims, checklist answers and evidence
  const claimAnchors = (c: Claim) => items[anchorKey(c.id)];
  const evAnchors = (c: Claim, e: Evidence1) => items[anchorKey(c.id, e.id)];
  const checklistAnchors = (a: ChecklistAudit) => {
    const c = byChecklistKey.get(a.key);
    return c ? claimAnchors(c) : undefined;
  };
  const checklistEvAnchors = (a: ChecklistAudit, e: Evidence1) => {
    const c = byChecklistKey.get(a.key);
    return items[checklistKey(a.key, e.id)] ?? (c ? evAnchors(c, e) : undefined);
  };
  const place = (list: Anchor[] | undefined, e?: Evidence1): EvidencePlace => {
    if (!pdfOn || (e && isCodeEvidence(e))) return "none";
    if (list?.length) return "pdf";
    return e && !e.location ? "none" : "missing";
  };

  // ---- actions
  const selectClaim = useCallback(
    (c: Claim, opts: { expand?: boolean; toPaper?: boolean; via?: string } = {}) => {
      track("claim_open", { claim_id: c.id, verdict: c.verdict, is_main: !!c.is_main, via: opts.via ?? "list" });
      update({ claim: c.id, tab: "claims" });
      setExpanded(opts.expand ?? true);
      scrollTo(items[c.id], c.location.page);
      if (!wide && opts.toPaper && pdfOn) setOnPaper(true);
    },
    [update, scrollTo, items, wide, pdfOn],
  );
  const selectEvidence = (c: Claim, e: Evidence1) => {
    track("evidence_click", { claim_id: c.id, evidence_id: e.id, kind: e.kind, method: evAnchors(c, e)?.[0]?.method });
    update({ claim: c.id, ev: e.id });
    scrollTo(evAnchors(c, e), e.location?.page);
    if (!wide) setOnPaper(true);
  };
  const selectChecklist = (a: ChecklistAudit) => {
    track("claim_open", { claim_id: `checklist:${a.key}`, verdict: a.status, via: "checklist" });
    update({ claim: `checklist:${a.key}`, tab: "checklist" });
    const c = byChecklistKey.get(a.key);
    scrollTo(checklistAnchors(a), c?.location.page);
    if (!wide) setOnPaper(true);
  };
  const selectChecklistEvidence = (a: ChecklistAudit, e: Evidence1) => {
    track("evidence_click", { claim_id: `checklist:${a.key}`, evidence_id: e.id, kind: e.kind, method: checklistEvAnchors(a, e)?.[0]?.method });
    update({ claim: `checklist:${a.key}`, ev: e.id, tab: "checklist" });
    scrollTo(checklistEvAnchors(a, e), e.location?.page);
    if (!wide) setOnPaper(true);
  };

  // a shared link (?claim=&ev=) scrolls to its target once
  const first = useRef(true);
  useEffect(() => {
    if (!first.current) return;
    first.current = false;
    if (claim) {
      const e = sel.ev ? claim.evidence?.find((x) => x.id === sel.ev) : undefined;
      if (e) scrollTo(evAnchors(claim, e), e.location?.page);
      else scrollTo(items[claim.id], claim.location.page);
    } else if (checklistSel) {
      const a = r.checklist?.find((x) => x.key === checklistSel);
      if (a) scrollTo(checklistAnchors(a), byChecklistKey.get(a.key)?.location.page);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- keyboard: j/k or arrows move, Enter expands, Esc collapses
  const rowRefs = useRef(new Map<string, HTMLLIElement>());
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (sel.tab !== "claims" || ev.altKey || ev.ctrlKey || ev.metaKey) return;
      const t = ev.target as HTMLElement | null;
      if (t && (t.closest("input, textarea, select, [contenteditable=true]") || t.closest("[role=dialog]"))) return;
      const inPdf = !!t?.closest('[aria-label="Paper"], #view-panel-paper');
      const k = ev.key;
      if (k === "j" || k === "k" || ((k === "ArrowDown" || k === "ArrowUp") && !inPdf)) {
        const next = stepClaim(shown, claim?.id ?? null, k === "j" || k === "ArrowDown" ? 1 : -1);
        const c = shown.find((x) => x.id === next);
        if (c) {
          ev.preventDefault();
          selectClaim(c, { expand: false, via: "keyboard" });
          rowRefs.current.get(c.id)?.querySelector("button")?.focus({ preventScroll: true });
          rowRefs.current.get(c.id)?.scrollIntoView({ block: "nearest" });
        }
      } else if (k === "Enter" && claim && !(t instanceof HTMLButtonElement) && !(t instanceof HTMLAnchorElement)) {
        ev.preventDefault();
        setExpanded(true);
      } else if (k === "Escape") {
        if (expanded) setExpanded(false);
        else if (sel.ev) update({ ev: null });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sel.tab, sel.ev, shown, claim, expanded, selectClaim, update]);

  // ---- what the PDF shows
  const marks = useMemo<PdfMark[]>(() => {
    const out: PdfMark[] = [];
    for (const c of shown) {
      const f = firstRect(items[c.id]);
      if (!f?.rect) continue;
      const q = c.quote.length > 90 ? `${c.quote.slice(0, 90).trimEnd()}…` : c.quote;
      out.push({ claimId: c.id, verdict: c.verdict, page: f.page, rect: f.rect, label: `${VERDICTS[c.verdict].label}: “${q}”` });
    }
    return out;
  }, [shown, items]);

  let selected: { verdict: Verdict; highlights: PdfHighlight[] } | null = null;
  let evidence: PdfHighlight[] | null = null;
  let checklistClaim: Claim | undefined;
  if (claim) {
    selected = { verdict: claim.verdict, highlights: claimAnchors(claim) ?? [] };
    const e = sel.ev ? claim.evidence?.find((x) => x.id === sel.ev) : undefined;
    if (e) evidence = evAnchors(claim, e) ?? null;
  } else if (checklistSel) {
    const a = r.checklist?.find((x) => x.key === checklistSel);
    checklistClaim = byChecklistKey.get(checklistSel);
    if (a && checklistClaim) selected = { verdict: checklistClaim.verdict, highlights: checklistAnchors(a) ?? [] };
    const e = a && sel.ev ? a.evidence?.find((x) => x.id === sel.ev) : undefined;
    if (a && e) evidence = checklistEvAnchors(a, e) ?? null;
  }

  const pdf = noPdf ? null : (
    <Suspense fallback={<Loading label="Loading the paper viewer…" />}>
      <PdfSlot
        id={r.analysis.id}
        anchors={anchors}
        done
        problem={problem}
        onProblem={onProblem}
        marks={marks}
        selected={selected}
        evidence={evidence}
        scroll={scroll}
        onMark={(cid) => {
          const c = claims.find((x) => x.id === cid);
          if (!c) return;
          track("margin_mark_click", { claim_id: c.id, verdict: c.verdict });
          selectClaim(c, { expand: true, via: "margin" });
          requestAnimationFrame(() => rowRefs.current.get(c.id)?.scrollIntoView({ block: "nearest", behavior: "smooth" }));
        }}
        active={wide || onPaper}
      />
    </Suspense>
  );

  const report = (
    <div className="px-4 py-4 sm:px-5">
      <ReportHeader r={r} claims={claims} filter={filter} setFilter={setFilter} />
      <div className="mt-3">
        <Tabs
          idPrefix="report"
          active={sel.tab}
          onChange={(t) => {
            track("tab_switch", { tab: t });
            update({ tab: t as ReportTab, claim: null });
          }}
          tabs={[
            { id: "claims", label: "Claims", count: claims.length },
            { id: "checklist", label: "Checklist", count: (r.checklist ?? []).length },
            { id: "code", label: "Code" },
            { id: "not-checked", label: "Not checked" },
          ]}
        />
      </div>
      <TabPanel idPrefix="report" id="claims" active={sel.tab}>
        <p className="mb-2 text-xs text-slate-600" role="status">
          {shown.length} of {claims.length} claims, in paper order. <span className="hidden lg:inline">Keys: j / k to move, Enter to open, Esc to close.</span>
        </p>
        <ul aria-label="Claims" className="divide-y divide-slate-200 border-y border-slate-200">
          {shown.map((c) => {
            const isSel = claim?.id === c.id;
            const open = isSel && expanded;
            return (
              <li
                key={c.id}
                ref={(el) => {
                  if (el) rowRefs.current.set(c.id, el);
                  else rowRefs.current.delete(c.id);
                }}
                className={isSel ? "bg-slate-50" : ""}
              >
                <button
                  type="button"
                  aria-expanded={open}
                  aria-current={isSel ? "true" : undefined}
                  onClick={() => (open ? setExpanded(false) : selectClaim(c, { expand: true, toPaper: true }))}
                  className={`grid w-full grid-cols-[auto_1fr] items-start gap-x-3 gap-y-1 px-2 py-2.5 text-left hover:bg-slate-50 ${isSel ? "shadow-[inset_3px_0_0_0_#0f172a]" : ""}`}
                >
                  <VerdictBadge verdict={c.verdict} />
                  <span className="min-w-0">
                    <span className="line-clamp-2 text-sm text-slate-900 break-anywhere">{c.quote}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">
                      p. {c.location.page}
                      {c.is_main ? " · main" : ""}
                      {c.stability && (
                        <span title={`${c.stability.agree} of ${c.stability.runs} runs gave this verdict`}>
                          {" "}· {c.stability.agree}/{c.stability.runs} runs agree
                        </span>
                      )}
                    </span>
                  </span>
                </button>
                {open && (
                  <div className="px-2 pb-4 pt-1">
                    <ClaimPanel
                      claim={c}
                      analysisId={r.analysis.id}
                      placeOf={(e) => place(evAnchors(c, e), e)}
                      selectedEvidence={sel.ev}
                      onEvidence={(e) => selectEvidence(c, e)}
                      quoteMissing={pdfOn && !claimAnchors(c)?.length}
                    />
                    {!wide && pdfOn && (
                      <button type="button" onClick={() => selectClaim(c, { toPaper: true })} className="mt-3 text-sm text-blue-900 underline">
                        Show the claim in the paper
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        {!shown.length && <p className="py-4 text-sm text-slate-600">No claim matches these filters.</p>}
      </TabPanel>
      <TabPanel idPrefix="report" id="checklist" active={sel.tab}>
        <ChecklistSection
          r={r}
          selectedKey={checklistSel}
          selectedEvidence={sel.ev}
          onSelect={selectChecklist}
          onEvidence={selectChecklistEvidence}
          placeOfItem={(a) => (byChecklistKey.has(a.key) ? place(checklistAnchors(a)) : "none")}
          placeOfEvidence={(a, e) => place(checklistEvAnchors(a, e), e)}
        />
      </TabPanel>
      <TabPanel idPrefix="report" id="code" active={sel.tab}>
        <CodeSection r={r} />
      </TabPanel>
      <TabPanel idPrefix="report" id="not-checked" active={sel.tab}>
        <NotCheckedSection r={r} />
      </TabPanel>
    </div>
  );

  return (
    <SplitLayout wide={wide} pdf={pdf} report={report} onPaper={onPaper} setOnPaper={setOnPaper} canGoBack />
  );
}

// ---------------------------------------------------------------- header, filters, export

function ReportHeader({ r, claims, filter, setFilter }: {
  r: AnalysisResponse;
  claims: Claim[];
  filter: ClaimFilter;
  setFilter: (f: ClaimFilter) => void;
}) {
  const title = reportTitle(r);
  const authors = authorsOf(r);
  const counts = useMemo(() => {
    const c: Partial<Record<Verdict, number>> = {};
    for (const x of claims) c[x.verdict] = (c[x.verdict] ?? 0) + 1;
    return c;
  }, [claims]);
  return (
    <header>
      <div className="flex items-start gap-3">
        <h1 className="min-w-0 flex-1 text-xl font-semibold leading-snug tracking-tight break-anywhere">{title}</h1>
        <ExportMenu r={r} title={title} />
      </div>
      {authors && <p className="mt-1 text-sm text-slate-700 break-anywhere">{authors}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-600">
        {r.analysis.source_url && (
          <a href={r.analysis.source_url} target="_blank" rel="noreferrer" onClick={() => track("outbound_click", { target: "source" })} className="inline-flex items-center gap-1 text-blue-900 underline">
            Source <IconExternal />
            <span className="sr-only">(opens in a new tab)</span>
          </a>
        )}
        <span>
          {r.mode === "live" ? liveModeLabel(r.code_stage) : `Analysis, ${r.runs} run${r.runs === 1 ? "" : "s"}`} · {date(r.created_at)}
        </span>
        <VersionBadge version={r.onus_version} fingerprint={r.fingerprint} label="report" />
        <OlderVersionBadge fingerprint={r.fingerprint} version={r.onus_version} />
      </div>
      <CodeStageNote stage={r.code_stage} />

      <fieldset className="mt-3">
        <legend className="sr-only">Show verdicts</legend>
        <div className="flex flex-wrap gap-1.5">
          {ALL_VERDICTS.map((v) => {
            const on = filter.verdicts.has(v);
            const { Icon, label, fill } = VERDICTS[v];
            return (
              <button
                key={v}
                type="button"
                aria-pressed={on}
                onClick={() => {
                  track("filter", { kind: "verdict", value: v, on: !on });
                  setFilter({ ...filter, verdicts: toggleVerdict(filter.verdicts, v) });
                }}
                className={`inline-flex items-center gap-1 rounded border px-2 py-1 text-xs ${
                  on ? "border-slate-400 bg-white text-slate-900" : "border-dashed border-slate-300 bg-slate-50 text-slate-500 line-through"
                }`}
                title={on ? `Hide ${label.toLowerCase()} claims` : `Show ${label.toLowerCase()} claims`}
              >
                <Icon style={{ color: on ? fill : undefined }} />
                {label}
                <span className="tabular-nums font-medium">{counts[v] ?? 0}</span>
              </button>
            );
          })}
        </div>
      </fieldset>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <label className="flex min-w-0 flex-1 items-center gap-2 text-sm">
          <span className="sr-only">Search quotes</span>
          <input
            type="search"
            value={filter.text}
            onChange={(e) => setFilter({ ...filter, text: e.target.value })}
            placeholder="Search quotes"
            className="w-full min-w-0 rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="inline-flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={filter.mainOnly}
            onChange={(e) => {
              track("filter", { kind: "main_only", on: e.target.checked });
              setFilter({ ...filter, mainOnly: e.target.checked });
            }}
          />
          Main claims only
        </label>
      </div>
      <p className="mt-2 text-xs text-slate-500">Flags mark evidence gaps in the paper, not misconduct. There is no overall score.</p>
    </header>
  );
}

function download(name: string, type: string, text: string) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ExportMenu({ r, title }: { r: AnalysisResponse; title: string }) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [copied, setCopied] = useState<"ok" | "fail" | null>(null);
  const name = exportName(r.analysis.paper_ref, r.fingerprint);
  const md = () => reportMarkdown(r, title, r.analysis.source_url);
  const close = () => ref.current?.removeAttribute("open");
  const item = "block w-full rounded px-3 py-1.5 text-left text-sm hover:bg-slate-100";
  return (
    <details ref={ref} className="relative shrink-0">
      <summary className="cursor-pointer list-none rounded border border-slate-300 px-2.5 py-1 text-sm hover:bg-slate-50">Export</summary>
      <div className="absolute right-0 z-20 mt-1 w-52 rounded border border-slate-200 bg-white p-1 shadow-lg">
        <button
          type="button"
          className={item}
          onClick={() => {
            track("export", { format: "markdown_copy" });
            navigator.clipboard?.writeText(md()).then(
              () => setCopied("ok"),
              () => setCopied("fail"),
            );
            setTimeout(() => setCopied(null), 2500);
          }}
        >
          {copied === "ok" ? "Copied" : copied === "fail" ? "Could not copy" : "Copy as Markdown"}
        </button>
        <button type="button" className={item} onClick={() => (track("export", { format: "markdown" }), download(`${name}.md`, "text/markdown", md()), close())}>
          Download Markdown
        </button>
        <button
          type="button"
          className={item}
          onClick={() => {
            // the RunResult as served, without the page-only fields
            const { analysis: _a, display: _d, code_stage: _c, ...result } = r;
            void _a;
            void _d;
            void _c;
            track("export", { format: "json" });
            download(`${name}.json`, "application/json", JSON.stringify(result, null, 2));
            close();
          }}
        >
          Download JSON
        </button>
      </div>
    </details>
  );
}
