/**
 * Readable text for a failed POST /api/analyses. The server's `detail` is already plain language (limits.py,
 * routes_analyses.py); this picks a title and fills in a fallback when the body had no detail.
 */
export interface StartErrorText {
  title: string;
  body: string;
}

export const LIVE_OFF_TEXT =
  "Live analysis is switched off on this server for now. Reports that were already made still open, including the examples below.";

export function startErrorText(status: number, detail: string | null | undefined): StartErrorText {
  const d = detail && !/^Request failed \(HTTP \d+\)\.$/.test(detail) ? detail : null;
  if (status === 429) {
    return { title: "Limit reached", body: d ?? "Too many live analyses right now. Please try again later." };
  }
  if (status === 503) return { title: "Live analysis is off", body: d ?? LIVE_OFF_TEXT };
  if (status === 413) return { title: "File too large", body: d ?? "That PDF is too large." };
  if (status === 0) return { title: "Could not reach the server", body: d ?? "Check your connection and try again." };
  return { title: "Could not start the analysis", body: d ?? `The server answered with HTTP ${status}.` };
}
