import { EMPTY_ANCHORS, type Anchors } from "../lib/anchors";
import type { AnalysisResponse, AnalysisStatus, Meta, StartResponse } from "./types";

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

/** The analysis's anchors; no PDF (404, or `pdf: false`) gives empty anchors, never an error. */
async function anchors(id: string): Promise<Anchors> {
  try {
    const a = await request<Anchors>(`/api/analyses/${encodeURIComponent(id)}/anchors`);
    return { ...EMPTY_ANCHORS, ...a, items: a.items ?? {}, missing: a.missing ?? [], page_sizes: a.page_sizes ?? [] };
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return EMPTY_ANCHORS;
    throw e;
  }
}

export const api = {
  meta: () => request<Meta>("/api/meta"),
  analysis: (id: string) => request<AnalysisResponse>(`/api/analyses/${encodeURIComponent(id)}`),
  status: (id: string) => request<AnalysisStatus>(`/api/analyses/${encodeURIComponent(id)}/status`),
  anchors,
  /** The paper PDF (pdf.js fetches it; 404 {"detail":"no_pdf"} when the report has none). */
  pdfUrl: (id: string) => `/api/analyses/${encodeURIComponent(id)}/pdf`,
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
