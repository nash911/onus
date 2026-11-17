/**
 * Pure logic for the report pane: claim filters and search, the URL selection (?claim=&ev=&tab=), and the
 * Markdown export. Type-only imports so `node --test` can load it.
 */
import type { Claim, RunResult, Verdict } from "../types/result";

export const ALL_VERDICTS: Verdict[] = ["contradicted", "overstated", "untraced", "not_checkable", "supported"];
const FLAGGED: Verdict[] = ["contradicted", "overstated", "untraced"];
const LABEL: Record<Verdict, string> = {
  supported: "Supported",
  overstated: "Overstated",
  untraced: "Untraced",
  contradicted: "Contradicted",
  not_checkable: "Not checkable",
};

// ---------------------------------------------------------------- filters

export interface ClaimFilter {
  /** Verdicts shown; all on by default. */
  verdicts: ReadonlySet<Verdict>;
  /** Case-insensitive search over quotes. */
  text: string;
  mainOnly: boolean;
}

export const DEFAULT_FILTER: ClaimFilter = { verdicts: new Set(ALL_VERDICTS), text: "", mainOnly: false };

/** Paper order: page, then position in the text. */
export function paperOrder(claims: readonly Claim[]): Claim[] {
  return [...claims].sort(
    (a, b) => a.location.page - b.location.page || a.location.start - b.location.start || a.id.localeCompare(b.id),
  );
}

export function filterClaims(claims: readonly Claim[], f: ClaimFilter): Claim[] {
  const words = f.text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return paperOrder(claims).filter((c) => {
    if (!f.verdicts.has(c.verdict)) return false;
    if (f.mainOnly && !c.is_main) return false;
    const q = c.quote.toLowerCase();
    return words.every((w) => q.includes(w));
  });
}

export function toggleVerdict(set: ReadonlySet<Verdict>, v: Verdict): Set<Verdict> {
  const n = new Set(set);
  if (n.has(v)) n.delete(v);
  else n.add(v);
  return n;
}

/** The claim `step` rows away from `current` in the shown list (j/k); the first row when nothing is selected. */
export function stepClaim(shown: readonly Claim[], current: string | null, step: 1 | -1): string | null {
  if (!shown.length) return null;
  const i = current ? shown.findIndex((c) => c.id === current) : -1;
  if (i < 0) return shown[step === 1 ? 0 : shown.length - 1]!.id;
  return shown[Math.min(shown.length - 1, Math.max(0, i + step))]!.id;
}

// ---------------------------------------------------------------- URL selection

export const REPORT_TABS = ["claims", "checklist", "code", "not-checked"] as const;
export type ReportTab = (typeof REPORT_TABS)[number];

export interface Selection {
  claim: string | null;
  ev: string | null;
  tab: ReportTab;
}

export function readSelection(p: URLSearchParams): Selection {
  const tab = p.get("tab");
  const claim = p.get("claim") || null;
  return {
    claim,
    ev: claim ? p.get("ev") || null : null,
    tab: (REPORT_TABS as readonly string[]).includes(tab ?? "") ? (tab as ReportTab) : "claims",
  };
}

/** New params with the selection written in; other params are kept, defaults are left out. */
export function writeSelection(p: URLSearchParams, s: Partial<Selection>): URLSearchParams {
  const cur = readSelection(p);
  const next = { ...cur, ...s };
  if (s.claim !== undefined && s.claim !== cur.claim && s.ev === undefined) next.ev = null; // a new claim drops ev
  const n = new URLSearchParams(p);
  const set = (k: string, v: string | null) => (v ? n.set(k, v) : n.delete(k));
  set("claim", next.claim);
  set("ev", next.claim ? next.ev : null);
  set("tab", next.tab === "claims" ? null : next.tab);
  return n;
}

// ---------------------------------------------------------------- Markdown export

const oneLine = (s: string) => s.replace(/\s+/g, " ").trim();

export function reportMarkdown(r: RunResult, title: string, sourceUrl?: string | null): string {
  const claims = paperOrder(r.claims ?? []);
  const counts: Partial<Record<Verdict, number>> = {};
  for (const c of claims) counts[c.verdict] = (counts[c.verdict] ?? 0) + 1;
  const out: string[] = [`# ${oneLine(title)}`, ""];
  if (sourceUrl) out.push(`Source: ${sourceUrl}`, "");
  out.push(
    `Onus ${r.onus_version} (pipeline ${r.fingerprint}), ${r.mode} analysis, ${r.runs} run${r.runs === 1 ? "" : "s"}.`,
    "",
    "## Verdicts",
    "",
    ...ALL_VERDICTS.map((v) => `- ${LABEL[v]}: ${counts[v] ?? 0}`),
    "",
    "## Flagged claims",
    "",
  );
  const flagged = claims.filter((c) => FLAGGED.includes(c.verdict));
  if (!flagged.length) out.push("No claim was flagged.", "");
  flagged.forEach((c, i) => {
    out.push(`### ${i + 1}. ${LABEL[c.verdict]} (p. ${c.location.page})`, "", `> ${oneLine(c.quote)}`, "");
    out.push(`${oneLine(c.reason)}`);
    if (c.caveat) out.push("", `Caveat: ${oneLine(c.caveat)}`);
    const ev = (c.evidence ?? []).map((e) => oneLine(e.label)).filter(Boolean);
    if (ev.length) out.push("", `Evidence: ${Array.from(new Set(ev)).join("; ")}`);
    out.push("");
  });
  out.push("## Not checked", "");
  const nc = r.not_checked ?? [];
  if (!nc.length) out.push("Nothing recorded.");
  for (const n of nc) out.push(`- ${oneLine(n.what)}${(n.count ?? 1) > 1 ? ` (${n.count})` : ""}: ${oneLine(n.reason)}`);
  out.push("");
  return out.join("\n");
}

/** A safe file stem for exports. */
export function exportName(ref: string, fingerprint: string): string {
  return `onus_${ref.replace(/[^A-Za-z0-9._-]+/g, "_")}_${fingerprint}`;
}
