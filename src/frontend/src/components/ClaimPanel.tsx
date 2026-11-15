import { useMutation } from "@tanstack/react-query";
import { useId, useState } from "react";
import { api } from "../api/client";
import type { CheckResult, Claim, Evidence1 } from "../types/result";
import { CHECK_LABELS, LINK_METHOD_LABELS, cite, humanize } from "../lib/format";
import { VERDICTS, isVerdict } from "../lib/verdicts";
import { ErrorBox } from "./States";
import { VerdictBadge } from "./Verdict";

const OUTCOME: Record<string, { label: string; cls: string }> = {
  pass: { label: "pass", cls: "text-green-800" },
  fail: { label: "fail", cls: "text-red-800 font-medium" },
  inconclusive: { label: "inconclusive", cls: "text-zinc-600" },
  not_run: { label: "not run", cls: "text-zinc-500" },
};

const RELATION: Record<string, string> = {
  supports: "supports",
  partial: "partly supports",
  contradicts: "contradicts",
  unrelated: "unrelated",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
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

function EvidenceRow({ e }: { e: Evidence1 }) {
  return (
    <li className="py-3" id={`evidence-${e.id}`}>
      <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="font-mono text-xs text-slate-500">{e.id}</span>
        <span className="font-medium break-anywhere">{e.label}</span>
        {e.relation && (
          <span className={e.relation === "contradicts" ? "text-red-800" : "text-slate-600"}>
            {RELATION[e.relation] ?? e.relation}
          </span>
        )}
      </div>
      {e.snippet && (
        <blockquote className="mt-1 border-l-2 border-slate-200 pl-3 text-sm text-slate-700 whitespace-pre-wrap break-anywhere">
          {e.snippet}
        </blockquote>
      )}
      <p className="mt-1 text-xs text-slate-500">
        {[e.location ? cite(e.location) : null, e.location?.section, humanize(e.kind.replace("_", " ")), `linked by ${LINK_METHOD_LABELS[e.link_method] ?? e.link_method}`]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </li>
  );
}

function DisputeForm({ analysisId, claimId }: { analysisId: string; claimId: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [contact, setContact] = useState("");
  const ids = { reason: useId(), contact: useId() };
  const m = useMutation({
    mutationFn: () => api.dispute(analysisId, { claim_id: claimId, reason, contact: contact || undefined }),
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
        onClick={() => setOpen(true)}
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

export function ClaimPanel({ claim, analysisId }: { claim: Claim; analysisId: string }) {
  const v = VERDICTS[claim.verdict];
  const markers = claim.wording.markers ?? [];
  const evidence = claim.evidence ?? [];
  const checks = claim.checks ?? [];
  const changed = claim.rule_verdict && claim.rule_verdict !== claim.verdict && isVerdict(claim.rule_verdict);
  return (
    <article>
      <div className="flex flex-wrap items-center gap-2">
        <VerdictBadge verdict={claim.verdict} size="md" />
        <span className="text-sm text-slate-600">
          {humanize(claim.type)} claim{claim.is_main ? " · main claim" : ""}
        </span>
      </div>

      <blockquote className="mt-4 border-l-4 pl-4 text-base leading-relaxed text-slate-900 break-anywhere" style={{ borderColor: v.fill }}>
        “{claim.quote}”
      </blockquote>
      <p className="mt-2 text-sm text-slate-600">
        {cite(claim.location)}
        {claim.location.section ? ` · ${claim.location.section}` : ""}
      </p>

      <Section title="Verdict">
        <p className="text-slate-900">{claim.reason}</p>
        {claim.caveat && <p className="mt-2 text-sm text-slate-600">Caveat: {claim.caveat}</p>}
        {changed && (
          <p className="mt-2 text-sm text-slate-600">
            The rule-based verdict was {VERDICTS[claim.rule_verdict!].label.toLowerCase()}; the adjudicator changed it,
            citing the evidence above.
          </p>
        )}
        {claim.stability && (
          <p className="mt-2 text-sm text-slate-700">
            Stability: {claim.stability.agree}/{claim.stability.runs} runs agree
            <span className="text-slate-500">
              {" "}
              ({claim.stability.verdicts.map((x) => VERDICTS[x]?.label.toLowerCase() ?? x).join(", ")})
            </span>
          </p>
        )}
      </Section>

      <Section title="Wording">
        <p className="text-sm text-slate-700">
          Strength {claim.wording.strength} · scope {claim.wording.scope}
        </p>
        {markers.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {markers.map((m) => (
              <li key={m} className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
                {m}
              </li>
            ))}
          </ul>
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

      <Section title={`Evidence (${evidence.length})`}>
        {evidence.length ? (
          <ul className="divide-y divide-slate-100">
            {evidence.map((e) => (
              <EvidenceRow key={e.id} e={e} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-600">No evidence was located for this claim.</p>
        )}
      </Section>

      <Section title="Disagree?">
        <DisputeForm analysisId={analysisId} claimId={claim.id} />
      </Section>
    </article>
  );
}
