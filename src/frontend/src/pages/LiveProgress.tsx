import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { IconCircle, IconSpinner, IconSupported } from "../components/Icons";
import { ErrorBox, Loading, PageTitle } from "../components/States";

const LABELS: Record<string, string> = {
  fetch: "Downloading the PDF",
  ingest: "Reading the PDF: text, sections, tables, checklist",
  claims: "Finding the claims",
  evidence: "Linking each claim to its evidence",
  checks: "Running the checks",
  verdicts: "Deciding verdicts and writing reasons",
};
const ORDER = ["fetch", "ingest", "claims", "evidence", "checks", "verdicts"];

export default function LiveProgress() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const q = useQuery({
    queryKey: ["status", id],
    queryFn: () => api.status(id),
    refetchInterval: (query) => {
      const s = query.state.data?.status;
      return s === "done" || s === "failed" ? false : 1500;
    },
  });

  useEffect(() => {
    if (q.data?.status === "done") navigate(`/analyses/${id}`, { replace: true });
  }, [q.data?.status, id, navigate]);

  if (q.isPending) return <Loading />;
  if (q.error) return <ErrorBox title="Could not load the analysis status" error={q.error} />;
  const s = q.data;
  const p = s.progress;
  const history = p?.history ?? [];
  const stages = ORDER.filter((x) => x !== "fetch" || history.includes("fetch") || p?.stage === "fetch");
  const currentIdx = stages.indexOf(p?.stage ?? "");

  return (
    <div className="max-w-2xl">
      <PageTitle sub="Live analysis: one run, no code checks. This page updates by itself and opens the report when it is ready.">
        Analysing the paper
      </PageTitle>

      {s.status === "failed" ? (
        <>
          <ErrorBox title="The analysis did not finish" error={s.error ?? "Unknown error."} />
          <Link to="/" className="text-sm text-blue-900 underline">
            Back to the start page to try again
          </Link>
        </>
      ) : (
        <>
          {s.status === "queued" && (
            <p className="mb-4 text-slate-700" role="status">
              Waiting in the queue
              {typeof p?.info?.ahead === "number" && p.info.ahead > 0 ? ` (${p.info.ahead} ahead)` : ""}. Live analyses
              run one at a time.
            </p>
          )}
          <ol className="space-y-3" aria-live="polite">
            {stages.map((st, i) => {
              const done = s.status === "done" || (currentIdx >= 0 && i < currentIdx);
              const active = s.status === "running" && i === currentIdx;
              return (
                <li key={st} className="flex items-start gap-3">
                  <span className={`mt-0.5 ${done ? "text-green-700" : active ? "text-slate-900" : "text-slate-300"}`}>
                    {done ? <IconSupported /> : active ? <IconSpinner /> : <IconCircle />}
                  </span>
                  <span className={done ? "text-slate-700" : active ? "font-medium text-slate-900" : "text-slate-500"}>
                    {LABELS[st] ?? st}
                    <span className="sr-only">{done ? " (done)" : active ? " (in progress)" : " (waiting)"}</span>
                  </span>
                </li>
              );
            })}
          </ol>
          {s.status === "done" && (
            <p className="mt-6">
              <Link to={`/analyses/${id}`} className="text-blue-900 underline">
                Open the report
              </Link>
            </p>
          )}
        </>
      )}
    </div>
  );
}
