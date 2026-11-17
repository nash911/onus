import type { Location } from "../types/result";

/** "p. 7, ¶ 3, s. 2", plus ", line 322" (or ", lines 322–324") only when the PDF has review line numbers.
 * Mirrors Location.cite() in onus/schemas/result.py. */
export function cite(loc: Location | null | undefined): string {
  if (!loc) return "";
  let out = `p. ${loc.page}, ¶ ${loc.paragraph}, s. ${loc.sentence}`;
  if (loc.line_start != null) {
    out +=
      loc.line_end != null && loc.line_end !== loc.line_start
        ? `, lines ${loc.line_start}–${loc.line_end}`
        : `, line ${loc.line_start}`;
  }
  return out;
}

export function pct(x: number | null | undefined, digits = 0): string {
  if (x == null || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}




export function date(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 10);
}

export function humanize(s: string): string {
  const t = s.replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

export const CHECK_LABELS: Record<string, string> = {
  link: "Evidence link",
  strength: "Strength rubric",
  number: "Number recheck",
  dominance: "Dominance",
  checklist: "Checklist audit",
  scope: "Scope vs limitations",
  code_static: "Code (static)",
  code_rerun: "Code (re-run)",
};

export const LINK_METHOD_LABELS: Record<string, string> = {
  explicit_ref: "explicit reference",
  retrieval: "retrieval",
  llm_selected: "model-selected",
  code_search: "code search",
  sandbox: "sandbox re-run",
};

