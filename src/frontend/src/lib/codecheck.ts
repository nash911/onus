/**
 * Plain-language wording for the code re-run entries in RunResult.code.reruns (onus/codecheck/apply.py
 * display_rerun). `status` is the harness vocabulary (consistent, consistent_modified, inconsistent, ...);
 * `run_status` is the re-run's own status. Unknown statuses still render, with the raw value as the label.
 * Self-contained (type-only imports) so `node --test` can load it directly.
 */

export type Tone = "good" | "bad" | "caution" | "neutral";

export interface RerunView {
  status: string;
  label: string;
  explain: string;
  tone: Tone;
  known: boolean;
  /** The line the unmodified code crashed on (fails_as_shipped, missing_data), when recorded. */
  errorLine: string | null;
  reason: string | null;
  minutes: number | null;
  commands: string[];
  modifications: { path: string; change: string; diff: string }[];
}

const STATUS: Record<string, { label: string; explain: string; tone: Tone }> = {
  consistent: {
    label: "Re-run matches the paper",
    explain: "The submitted code, run unmodified, printed values that match the paper's reported numbers.",
    tone: "good",
  },
  inconsistent: {
    label: "Re-run contradicts the paper",
    explain: "The submitted code, run unmodified, printed a value that does not match the paper's reported number.",
    tone: "bad",
  },
  consistent_modified: {
    label: "Matched only after the code was changed",
    explain:
      "The values matched the paper only after the submitted code was modified to make it run, so this does not count as a faithful re-run. The changes are listed below.",
    tone: "caution",
  },
  reproduced: {
    label: "Reproduced",
    explain: "The re-run produced values that were compared with the paper.",
    tone: "good",
  },
  unverified: {
    label: "Ran, but no value could be verified",
    explain: "The code ran, but none of its printed values could be checked against the paper.",
    tone: "neutral",
  },
  runs: {
    label: "Runs as submitted",
    explain: "The submission's own entry point ran to the end on the unmodified code. No reported number was compared.",
    tone: "good",
  },
  ran_no_values: {
    label: "Ran, but printed no comparable values",
    explain: "The code ran, but it did not print the numbers the paper reports.",
    tone: "neutral",
  },
  fails_as_shipped: {
    label: "Fails as submitted",
    explain: "The unmodified code stopped with an error.",
    tone: "bad",
  },
  missing_data: {
    label: "Data not included",
    explain:
      "The code needs data that is not part of the submission (for example hard-coded paths or missing input files).",
    tone: "caution",
  },
  install_failed: {
    label: "Dependencies would not install",
    explain: "Installing the code's requirements failed, so the code was not run.",
    tone: "caution",
  },
  needs_external: {
    label: "Needs resources that are not available here",
    explain: "The code needs hardware, services or models the re-run sandbox does not have.",
    tone: "neutral",
  },
  failed: {
    label: "Re-run ran out of steps",
    explain: "The re-run agent used up its step or time limit before it could finish. This says nothing about the code.",
    tone: "neutral",
  },
  skipped: {
    label: "Re-run skipped",
    explain: "A daily limit for code re-runs on this server was reached, so the code was not run. The static code check still ran.",
    tone: "neutral",
  },
  not_run: {
    label: "Not re-run",
    explain: "No re-run was attempted.",
    tone: "neutral",
  },
};

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

function errorLine(x: Record<string, unknown>): string | null {
  const shipped = x.as_shipped as Record<string, unknown> | null | undefined;
  const fromShipped = shipped && typeof shipped === "object" ? str(shipped.error_line) : null;
  if (fromShipped) return fromShipped;
  const log = Array.isArray(x.command_log) ? x.command_log : Array.isArray(x.commands) ? x.commands : [];
  for (const c of log) {
    if (c && typeof c === "object") {
      const line = str((c as Record<string, unknown>).error_line);
      if (line) return line;
    }
  }
  return null;
}

function commandText(c: unknown): string | null {
  if (typeof c === "string") return c;
  if (c && typeof c === "object") {
    const o = c as Record<string, unknown>;
    const cmd = str(o.cmd);
    if (!cmd) return null;
    return typeof o.rc === "number" ? `$ ${cmd}   # exit ${o.rc}` : `$ ${cmd}`;
  }
  return null;
}

export function rerunView(x: Record<string, unknown>): RerunView {
  const status = str(x.status) ?? str(x.run_status) ?? "unknown";
  // A raw "reproduced" from modified code is shown with the modified wording too.
  const key = status === "reproduced" && x.modified === true ? "consistent_modified" : status;
  const known = Object.hasOwn(STATUS, key);
  const s = known ? STATUS[key]! : { label: status, explain: "", tone: "neutral" as Tone };
  const mods = Array.isArray(x.modifications) ? x.modifications : [];
  return {
    status,
    label: s.label,
    explain: s.explain,
    tone: s.tone,
    known,
    errorLine: key === "fails_as_shipped" || key === "missing_data" ? errorLine(x) : null,
    reason: str(x.reason),
    minutes: typeof x.minutes === "number" && Number.isFinite(x.minutes) ? x.minutes : null,
    commands: (Array.isArray(x.commands) ? x.commands : []).map(commandText).filter((c): c is string => c != null),
    modifications: mods
      .filter((m): m is Record<string, unknown> => !!m && typeof m === "object")
      .map((m) => ({ path: str(m.path) ?? "(file)", change: str(m.change) ?? "", diff: typeof m.diff === "string" ? m.diff : "" })),
  };
}

const FACT: Record<string, string> = {
  present: "Found in the code",
  absent: "Not found in the code",
  contradicts: "Code contradicts the paper",
  unverified: "Could not be verified",
  not_run: "Not checked",
  consistent: "Paper and code agree",
  mismatch: "Paper and code disagree",
  not_checkable: "Not checkable",
};

/** Label for a static paper–code fact status; unknown values are shown as they are. */
export function factLabel(status: unknown): string {
  const s = str(status);
  if (!s) return "Unknown";
  return Object.hasOwn(FACT, s) ? FACT[s]! : s;
}
