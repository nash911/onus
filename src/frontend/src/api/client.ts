import type {
  AnalysisResponse,
  AnalysisStatus,
  Findings,
  Meta,
  Metrics,
  PaperDetail,
  PapersPage,
  StartResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "Could not reach the Onus server. Check your connection and try again.");
  }
  if (!res.ok) {
    let detail = `Request failed (HTTP ${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail) && body.detail[0]?.msg) detail = String(body.detail[0].msg);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** null on 404: findings and metrics do not exist until the held-out runs. */
async function optional<T>(path: string): Promise<T | null> {
  try {
    return await request<T>(path);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

// Dev-only: `npm run dev:fixtures` serves the dev split's files from var/eval (see vite.config.ts).
// `import.meta.env.DEV` is false in every build, so a build always reads /api.
const FIXTURES = import.meta.env.DEV && import.meta.env.VITE_DEV_FIXTURES === "1";

export const api = {
  meta: () => request<Meta>("/api/meta"),
  papers: (params: URLSearchParams) => request<PapersPage>(`/api/papers?${params.toString()}`),
  paper: (ref: string) => request<PaperDetail>(`/api/papers/${encodeURIComponent(ref)}`),
  analysis: (id: string) => request<AnalysisResponse>(`/api/analyses/${encodeURIComponent(id)}`),
  status: (id: string) => request<AnalysisStatus>(`/api/analyses/${encodeURIComponent(id)}/status`),
  findings: () => optional<Findings>(FIXTURES ? "/__dev/findings.json" : "/api/findings"),
  metrics: () => optional<Metrics>(FIXTURES ? "/__dev/metrics.json" : "/api/metrics"),
  /** An arXiv / OpenReview reference; with a code .zip it is sent as multipart (`ref` + `code`). */
  startRef: (body: { source: string; id: string } | { ref: string }, code?: File | null) => {
    if (code) {
      const form = new FormData();
      form.append("ref", "ref" in body ? body.ref : `${body.source}:${body.id}`);
      form.append("code", code);
      return request<StartResponse>("/api/analyses", { method: "POST", body: form });
    }
    return request<StartResponse>("/api/analyses", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    });
  },
  startUpload: (file: File, code?: File | null) => {
    const form = new FormData();
    form.append("file", file);
    if (code) form.append("code", code);
    return request<StartResponse>("/api/analyses", { method: "POST", body: form });
  },
  dispute: (analysisId: string, body: { claim_id: string; reason: string; contact?: string }) =>
    request<{ id: string; status: string }>(`/api/analyses/${encodeURIComponent(analysisId)}/disputes`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    }),
};
