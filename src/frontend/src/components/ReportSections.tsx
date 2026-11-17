/** The report's secondary tabs: Checklist, Code and Not checked. */
import type { AnalysisResponse } from "../api/types";
import { factLabel, rerunView, type RerunView } from "../lib/codecheck";
import { codeSourceText, codeStageLine, codeStagePending, type CodeStage } from "../lib/codeStage";
import { CHECK_LABELS, cite } from "../lib/format";
import type { ChecklistAudit, Evidence1, RunResult } from "../types/result";
import { isCodeEvidence, type EvidencePlace } from "./ClaimPanel";
import { IconSpinner } from "./Icons";
import { AuditBadge } from "./Verdict";

export function CodeStageNote({ stage }: { stage: CodeStage | null | undefined }) {
  const line = codeStageLine(stage);
  if (!line || !stage) return null;
  const pending = codeStagePending(stage);
  return (
    <p
      className={`mt-2 inline-flex items-start gap-2 rounded border px-3 py-2 text-sm ${
        pending ? "border-blue-200 bg-blue-50 text-blue-900" : "border-slate-200 bg-slate-50 text-slate-700"
      }`}
      role="status"
      aria-live="polite"
    >
      {pending && <IconSpinner />}
      <span className="break-anywhere">{line}</span>
    </p>
  );
}

// ---------------------------------------------------------------- checklist

export function ChecklistSection({ r, selectedKey, selectedEvidence, onSelect, onEvidence, placeOfItem, placeOfEvidence }: {
  r: RunResult;
  selectedKey: string | null;
  selectedEvidence: string | null;
  onSelect: (a: ChecklistAudit) => void;
  onEvidence: (a: ChecklistAudit, e: Evidence1) => void;
  placeOfItem: (a: ChecklistAudit) => EvidencePlace;
  placeOfEvidence: (a: ChecklistAudit, e: Evidence1) => EvidencePlace;
}) {
  const items = r.checklist ?? [];
  if (!items.length) return <p className="text-sm text-slate-600">No paper checklist was found in this PDF.</p>;
  return (
    <div>
      <p className="mb-2 text-sm text-slate-600">
        Each answer in the paper's checklist, and whether the paper itself backs it. "No" and "NA" answers are not
        applicable.
      </p>
      <ul className="divide-y divide-slate-200 border-y border-slate-200">
        {items.map((a) => {
          const place = placeOfItem(a);
          const sel = selectedKey === a.key;
          return (
            <li key={a.key} className={`py-3 ${sel ? "bg-slate-50" : ""}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{a.title}</span>
                <span className="text-sm text-slate-600">Answer: {a.answer}</span>
                <AuditBadge status={a.status} />
                {place !== "none" && (
                  <button type="button" onClick={() => onSelect(a)} aria-pressed={sel} className="ml-auto rounded px-2 py-0.5 text-xs text-blue-900 underline hover:bg-slate-100">
                    {sel ? "Shown in the paper" : "Show in the paper"}
                  </button>
                )}
              </div>
              {sel && place === "missing" && <p className="mt-1 text-xs font-medium text-amber-900">Exact place not found.</p>}
              <p className="mt-1 text-sm text-slate-700 break-anywhere">{a.reason}</p>
              {a.justification && (
                <p className="mt-1 text-xs text-slate-500 break-anywhere">Authors' justification: {a.justification}</p>
              )}
              {(a.evidence ?? []).length > 0 && (
                <ul className="mt-2 space-y-1">
                  {(a.evidence ?? []).map((e) => {
                    const ep = placeOfEvidence(a, e);
                    const on = sel && selectedEvidence === e.id;
                    const inner = (
                      <>
                        <span className="font-medium break-anywhere">{e.label}</span>
                        {e.location && <span className="text-xs text-slate-500"> · {cite(e.location)}</span>}
                        <span className={`block text-slate-700 break-anywhere line-clamp-4 ${isCodeEvidence(e) ? "font-mono text-xs" : ""}`}>{e.snippet}</span>
                        {on && ep === "missing" && <span className="block text-xs font-medium text-amber-900">Exact place not found.</span>}
                      </>
                    );
                    return (
                      <li key={e.id} className="border-l-2 border-slate-200 pl-3 text-sm">
                        {ep === "none" ? (
                          inner
                        ) : (
                          <button type="button" onClick={() => onEvidence(a, e)} aria-pressed={on} className={`block w-full rounded px-1 py-1 text-left ${on ? "bg-slate-100 outline-2 outline-dashed outline-slate-700" : "hover:bg-slate-50"}`}>
                            {inner}
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- code

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
      {v.note && (
        <p className="mt-1 text-xs text-slate-600 break-anywhere">
          <span className="font-medium">Re-run agent's note:</span> {v.note}
        </p>
      )}
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
        <details className="mt-2" open>
          <summary className="cursor-pointer text-blue-900">Commands run ({v.commands.length})</summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs break-anywhere">
            {v.commands.join("\n")}
          </pre>
        </details>
      )}
    </li>
  );
}

export function CodeSection({ r }: { r: AnalysisResponse }) {
  const stage = r.code_stage ?? null;
  const code = r.code;
  const statics = code.static ?? [];
  const reruns = code.reruns ?? [];
  const source = codeSourceText(stage);
  return (
    <div className="space-y-3 text-sm text-slate-700">
      <p>
        {code.available ? `${stage ? "Code" : "Submitted code"}: ${code.files} files.` : "No code was submitted with this paper."}
        {code.code_version ? ` Code checks version ${code.code_version}.` : ""}
      </p>
      {source && <p className="break-anywhere">{source}</p>}
      {r.mode === "live" && !stage && <p>This analysis did not run code checks.</p>}
      {stage && codeStageLine(stage) && <CodeStageNote stage={stage} />}
      {!statics.length && !reruns.length && code.available && r.mode !== "live" && (
        <p>Code checks are not part of this report's version.</p>
      )}
      {statics.length > 0 && (
        <section>
          <h3 className="font-semibold text-slate-900">What the paper says and what the code does</h3>
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
          <h3 className="font-semibold text-slate-900">Re-run receipt</h3>
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

// ---------------------------------------------------------------- not checked

/** Ledger reason keys are short codes; say them in words where we know them. */
function reasonText(k: string): string {
  return k.replace(/_/g, " ");
}

export function NotCheckedSection({ r }: { r: RunResult }) {
  const ledger = r.ledger ?? [];
  const notChecked = r.not_checked ?? [];
  const skipped = ledger.filter((l) => l.not_run_on > 0);
  return (
    <div className="space-y-6 text-sm">
      <section>
        <h3 className="font-semibold">What this report did not check</h3>
        {notChecked.length ? (
          <ul className="mt-2 divide-y divide-slate-100">
            {notChecked.map((n, i) => (
              <li key={i} className="py-2">
                <span className="font-medium">{n.what}</span>
                {(n.count ?? 1) > 1 && <span className="tabular-nums text-slate-500"> ({n.count})</span>}
                <span className="text-slate-600">: {n.reason}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-slate-600">Nothing was left out.</p>
        )}
      </section>
      <section>
        <h3 className="font-semibold">Checks that did not run on every claim</h3>
        {skipped.length ? (
          <ul className="mt-2 divide-y divide-slate-100">
            {skipped.map((l) => (
              <li key={l.check} className="py-2">
                <span className="font-medium">{CHECK_LABELS[l.check] ?? l.check}</span>
                <span className="text-slate-600">
                  {" "}
                  ran on {l.ran_on} claim{l.ran_on === 1 ? "" : "s"} and did not run on {l.not_run_on}.
                </span>
                {Object.keys(l.reasons ?? {}).length > 0 && (
                  <ul className="mt-1 list-disc pl-5 text-slate-600">
                    {Object.entries(l.reasons ?? {}).map(([k, n]) => (
                      <li key={k}>
                        {reasonText(k)}: <span className="tabular-nums">{n}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-slate-600">Every check ran wherever it applies.</p>
        )}
      </section>
      {(r.warnings ?? []).length > 0 && (
        <section>
          <h3 className="font-semibold">Warnings</h3>
          <ul className="mt-2 list-disc pl-5 text-slate-700">
            {(r.warnings ?? []).map((w, i) => (
              <li key={i} className="break-anywhere">{w}</li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
