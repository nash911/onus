/** The live analysis stages, shown in the report pane while the analysis runs (was pages/LiveProgress). */
import { Link } from "react-router-dom";
import type { AnalysisStatus } from "../api/types";
import { IconCircle, IconSpinner, IconSupported } from "./Icons";
import { ErrorBox } from "./States";

const LABELS: Record<string, string> = {
  fetch: "Downloading the PDF",
  ingest: "Reading the PDF: text, sections, tables, checklist",
  claims: "Finding the claims",
  evidence: "Linking each claim to its evidence",
  checks: "Running the checks",
  verdicts: "Deciding verdicts and writing reasons",
};
const ORDER = ["fetch", "ingest", "claims", "evidence", "checks", "verdicts"];

export function ProgressStages({ s, codeChecks }: { s: AnalysisStatus; codeChecks: boolean }) {
  const p = s.progress;
  const history = p?.history ?? [];
  const stages = ORDER.filter((x) => x !== "fetch" || history.includes("fetch") || p?.stage === "fetch");
  const currentIdx = stages.indexOf(p?.stage ?? "");

  if (s.status === "failed") {
    return (
      <div>
        <ErrorBox title="The analysis did not finish" error={s.error ?? "Unknown error."} />
        <Link to="/" className="text-sm text-blue-900 underline">
          Back to the start page to try again
        </Link>
      </div>
    );
  }
  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Analysing the paper</h1>
      <p className="mt-2 text-sm text-slate-600">
        One run; usually a few minutes. The report opens here by itself when it is ready
        {codeChecks ? "; code checks run after that and the report updates in place." : "."}
      </p>
      {s.status === "queued" && (
        <p className="mt-4 text-slate-700" role="status">
          Waiting in the queue
          {typeof p?.info?.ahead === "number" && p.info.ahead > 0 ? ` (${p.info.ahead} ahead)` : ""}. Live analyses
          run one at a time.
        </p>
      )}
      <ol className="mt-4 space-y-3" aria-live="polite">
        {stages.map((st, i) => {
          const done = s.status === "done" || (currentIdx >= 0 && i < currentIdx);
          const active = s.status === "running" && i === currentIdx;
          return (
            <li key={st} className="flex items-start gap-3">
              <span className={`mt-0.5 ${done ? "text-green-700" : active ? "text-slate-900" : "text-slate-400"}`}>
                {done ? <IconSupported /> : active ? <IconSpinner /> : <IconCircle />}
              </span>
              <span className={done ? "text-slate-700" : active ? "font-medium text-slate-900" : "text-slate-600"}>
                {LABELS[st] ?? st}
                <span className="sr-only">{done ? " (done)" : active ? " (in progress)" : " (waiting)"}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
