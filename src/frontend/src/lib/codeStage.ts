/**
 * Wording for the live code-check stage (`code_stage` on GET /api/analyses/{id}, api/code_stage.py). The P0
 * report is shown first; while the stage is queued or running the report says so and polls, then updates in place.
 * Self-contained (type-only imports) so `node --test` can load it directly.
 */

export interface CodeStageSource {
  kind?: "upload" | "repo" | "none";
  url?: string;
  host?: string;
  fetched_at?: string;
  unsupported?: string[];
}

export interface CodeStage {
  status: "queued" | "running" | "done" | "failed" | "skipped" | "no_code" | string;
  message?: string | null;
  source?: CodeStageSource | null;
  files?: number;
  static?: { status?: string; reason?: string } | null;
  rerun?: { attempted?: boolean; skipped?: string | null; minutes?: number; status?: string } | null;
  started_at?: string | null;
  finished_at?: string | null;
}

/** True while the report should keep polling for the code stage. */
export function codeStagePending(s: CodeStage | null | undefined): boolean {
  return !!s && (s.status === "queued" || s.status === "running");
}

/** Where the checked code came from, or null when no code was used. */
export function codeSourceText(s: CodeStage | null | undefined): string | null {
  const src = s?.source;
  if (!src || !src.kind || src.kind === "none") return null;
  if (src.kind === "upload") return "Code uploaded with the submission.";
  if (src.kind === "repo" && src.url) {
    const when = src.fetched_at ? `, default branch as of ${src.fetched_at}` : ", default branch";
    return `Code from ${src.url}${when}; this may differ from the code the paper was submitted with.`;
  }
  return null;
}

/** One status line for the report header and the Code tab, or null when there is nothing to say. */
export function codeStageLine(s: CodeStage | null | undefined): string | null {
  if (!s) return null;
  switch (s.status) {
    case "queued":
      return "Code checks waiting in the queue… The report below is complete and updates when they finish.";
    case "running":
      return "Code checks running… The report below is complete and updates when they finish.";
    case "failed":
    case "skipped":
      return s.message ?? (s.status === "failed" ? "The code checks did not finish." : "Code checks were skipped.");
    case "no_code":
      return s.message ?? null;
    default:
      return null;
  }
}

/** The header's mode label for a live report. */
export function liveModeLabel(s: CodeStage | null | undefined): string {
  if (s?.status === "done") return "Live analysis, 1 run, with code checks";
  if (codeStagePending(s)) return "Live analysis, 1 run, code checks running";
  return "Live analysis, 1 run, no code checks";
}
