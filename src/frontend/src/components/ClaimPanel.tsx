import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api } from "../api/client";
import type { CheckResult, Claim, Evidence1 } from "../types/result";
import { CHECK_LABELS, LINK_METHOD_LABELS, cite, humanize } from "../lib/format";
import { VERDICTS, isVerdict } from "../lib/verdicts";
import { ErrorBox } from "./States";
import { track } from "../lib/track";
import { VerdictBadge } from "./Verdict";

const OUTCOME: Record<string, { label: string; cls: string }> = {
  pass: { label: "pass", cls: "text-v-supported" },
  fail: { label: "fail", cls: "text-v-contradicted font-medium" },
  inconclusive: { label: "inconclusive", cls: "text-slate-600" },
  not_run: { label: "not run", cls: "text-slate-500" },
};

const RELATION: Record<string, string> = {
  supports: "supports",
  partial: "partly supports",
  contradicts: "contradicts",
  unrelated: "unrelated",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      <div className="mt-2">{children}</div>
    </section>
  );
}

function CheckRow({ c }: { c: CheckResult }) {
  const o = OUTCOME[c.outcome] ?? { label: c.outcome, cls: "" };
  return (
    <li className="py-2">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{CHECK_LABELS[c.check] ?? c.check}</span>
        <span className={o.cls}>{o.label}</span>
        {c.severity && c.severity !== "none" && (
          <span className="text-xs text-slate-600">({c.severity === "contradicts" ? "contradicts" : "overstates"})</span>
        )}
      </div>
      <p className="mt-0.5 whitespace-pre-wrap text-sm text-slate-700 break-anywhere">{c.detail}</p>
      {c.evidence_ids && c.evidence_ids.length > 0 && (
        <p className="mt-0.5 text-xs text-slate-500">Evidence: {c.evidence_ids.join(", ")}</p>
      )}
    </li>
  );
}

/** Where an evidence item can be shown: in the PDF, not found there, or never (code, no location). */
export type EvidencePlace = "pdf" | "missing" | "none";

const CODE_KINDS = new Set(["code", "rerun_output"]);

export function isCodeEvidence(e: Evidence1): boolean {
  return CODE_KINDS.has(e.kind);
}

function EvidenceRow({ e, place, selected, onSelect }: {
  e: Evidence1;
  place: EvidencePlace;
  selected: boolean;
  onSelect?: (e: Evidence1) => void;
}) {
  const code = isCodeEvidence(e);
  const meta = [
    e.location ? cite(e.location) : null,
    e.location?.section,
    humanize(e.kind.replace("_", " ")),
    `linked by ${LINK_METHOD_LABELS[e.link_method] ?? e.link_method}`,
  ].filter(Boolean).join(" · ");
  const head = (
    <span className="flex flex-wrap items-baseline gap-x-2 text-sm">
      <span className="font-mono text-xs text-slate-500">{e.id}</span>
      <span className={`font-medium break-anywhere ${code ? "font-mono text-xs" : ""}`}>{e.label}</span>
      {e.relation && (
        <span className={e.relation === "contradicts" ? "text-v-contradicted" : "text-slate-600"}>
          {RELATION[e.relation] ?? e.relation}
        </span>
      )}
    </span>
  );
  const body = (
    <>
      {e.snippet &&
        (code ? (
          <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs text-slate-800 break-anywhere">
            {e.snippet}
          </pre>
        ) : (
          <span className="mt-1 block border-l-2 border-slate-200 pl-3 text-sm text-slate-700 whitespace-pre-wrap break-anywhere line-clamp-6">
            {e.snippet}
          </span>
        ))}
      <span className="mt-1 block text-xs text-slate-500">{meta}</span>
      {place === "missing" && (
        <span className="mt-1 block text-xs font-medium text-amber-900">Exact place not found; the PDF is open at its page.</span>
      )}
    </>
  );
  if (place === "none" || !onSelect) {
    return (
      <li className="py-3" id={`evidence-${e.id}`}>
        {head}
        {body}
        {code && <span className="mt-1 block text-xs text-slate-500">Code evidence is shown here, not in the paper.</span>}
      </li>
    );
  }
  return (
    <li className="py-1" id={`evidence-${e.id}`}>
      <button
        type="button"
        onClick={() => onSelect(e)}
        aria-pressed={selected}
        className={`block w-full rounded px-2 py-2 text-left ${selected ? "bg-slate-100 outline-2 outline-dashed outline-slate-700" : "hover:bg-slate-50"}`}
      >
        {head}
        {body}
        <span className="mt-1 block text-xs text-blue-900">{selected ? "Shown in the paper" : "Show in the paper"}</span>
      </button>
    </li>
  );
}

function DisputeForm({ analysisId, claimId }: { analysisId: string; claimId: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [contact, setContact] = useState("");
  const ids = { reason: useId(), contact: useId() };
  const m = useMutation({
    mutationFn: () => {
      track("dispute_submit", { claim_id: claimId, with_contact: !!contact });
      return api.dispute(analysisId, { claim_id: claimId, reason, contact: contact || undefined });
    },
  });
  if (m.isSuccess) {
    return (
      <p className="text-sm text-slate-700" role="status">
        Thank you. Your dispute is recorded as ticket <span className="font-mono">{m.data.id.slice(0, 8)}</span>.
        Disputes are reviewed by hand; they never retrain Onus automatically.
      </p>
    );
  }
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => {
          setOpen(true);
          track("dispute_open", { claim_id: claimId });
        }}
        className="rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50"
      >
        Dispute this verdict
      </button>
    );
  }
  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (reason.trim().length >= 3) m.mutate();
      }}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor={ids.reason} className="text-sm font-medium">
          What is wrong with this verdict?
        </label>
        <textarea
          id={ids.reason}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={4}
          maxLength={4000}
          required
          autoFocus
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
          placeholder="For example: Table 3 reports the number on page 8."
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={ids.contact} className="text-sm font-medium">
          Contact (optional)
        </label>
        <input
          id={ids.contact}
          value={contact}
          onChange={(e) => setContact(e.target.value)}
          maxLength={255}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
          placeholder="Email, if you want a reply"
        />
      </div>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={m.isPending || reason.trim().length < 3}
          className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {m.isPending ? "Sending…" : "Send dispute"}
        </button>
        <button type="button" onClick={() => setOpen(false)} className="rounded px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100">
          Cancel
        </button>
      </div>
      {m.error && <ErrorBox title="The dispute was not sent" error={m.error} />}
    </form>
  );
}

function AdjudicationNote({ claim }: { claim: Claim }) {
  const a = claim.adjudication as { decision?: string; note?: string; applied?: boolean } | null | undefined;
  const changed = claim.rule_verdict && claim.rule_verdict !== claim.verdict && isVerdict(claim.rule_verdict);
  const note = typeof a?.note === "string" ? a.note.trim() : "";
  if (!changed && !note) return null;
  return (
    <p className="mt-2 text-sm text-slate-600">
      {changed && (
        <>
          The rule-based verdict was {VERDICTS[claim.rule_verdict!].label.toLowerCase()}; the adjudicator changed it,
          citing the evidence below.{" "}
        </>
      )}
      {note && <span className="break-anywhere">Adjudicator's note: {note}</span>}
    </p>
  );
}

export interface ClaimPanelProps {
  claim: Claim;
  analysisId: string;
  /** Where each evidence item can be shown (default: nowhere, no buttons). */
  placeOf?: (e: Evidence1) => EvidencePlace;
  selectedEvidence?: string | null;
  onEvidence?: (e: Evidence1) => void;
  /** The claim's own quote was not found in the PDF. */
  quoteMissing?: boolean;
}

/** The detail of one claim, shown in place when its row in the report is expanded. */
export function ClaimPanel({ claim, analysisId, placeOf, selectedEvidence, onEvidence, quoteMissing }: ClaimPanelProps) {
  const markers = claim.wording.markers ?? [];
  const evidence = claim.evidence ?? [];
  const checks = claim.checks ?? [];
  return (
    <article className="text-slate-900">
      <p className="text-sm text-slate-600">
        {humanize(claim.type)} claim{claim.is_main ? " · main claim" : ""} · {cite(claim.location)}
        {claim.location.section ? ` · ${claim.location.section}` : ""}
      </p>
      <blockquote className="mt-2 border-l-4 pl-3 text-sm leading-relaxed break-anywhere" style={{ borderColor: VERDICTS[claim.verdict].fill }}>
        “{claim.quote}”
      </blockquote>
      {quoteMissing && (
        <p className="mt-1 text-xs font-medium text-amber-900">Exact place not found; the PDF is open at page {claim.location.page}.</p>
      )}

      <Section title="Verdict">
        <div className="flex flex-wrap items-center gap-2">
          <VerdictBadge verdict={claim.verdict} />
        </div>
        <p className="mt-2">{claim.reason}</p>
        {claim.caveat && <p className="mt-2 text-sm text-slate-600">Caveat: {claim.caveat}</p>}
        <AdjudicationNote claim={claim} />
        {claim.stability && (
          <p className="mt-2 text-sm text-slate-700">
            Stability: {claim.stability.agree}/{claim.stability.runs} runs agree
            <span className="text-slate-500">
              {" "}
              ({claim.stability.verdicts.map((x) => VERDICTS[x]?.label.toLowerCase() ?? x).join(", ")})
            </span>
          </p>
        )}
        <p className="mt-2 text-xs text-slate-500">
          Wording: strength {claim.wording.strength}, scope {claim.wording.scope}
          {markers.length > 0 && ` (${markers.join(", ")})`}
        </p>
      </Section>

      <Section title={`Evidence (${evidence.length})`}>
        {evidence.length ? (
          <ul className="divide-y divide-slate-100">
            {evidence.map((e) => (
              <EvidenceRow
                key={e.id}
                e={e}
                place={placeOf ? placeOf(e) : "none"}
                selected={selectedEvidence === e.id}
                onSelect={onEvidence}
              />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-600">No evidence was located for this claim.</p>
        )}
      </Section>

      <Section title={`Checks (${checks.length})`}>
        {checks.length ? (
          <ul className="divide-y divide-slate-100">
            {checks.map((c, i) => (
              <CheckRow key={`${c.check}-${i}`} c={c} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-600">No checks ran on this claim.</p>
        )}
      </Section>

      <Section title="Disagree?">
        <DisputeForm analysisId={analysisId} claimId={claim.id} />
      </Section>
    </article>
  );
}
