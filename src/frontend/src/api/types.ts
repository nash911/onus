/**
 * Shapes returned by the Onus API around the RunResult contract. RunResult itself is generated from
 * schema/result.schema.json (`npm run gen:types` -> src/types/result.ts); the rest mirrors
 * onus/api/serialize.py and onus/api/routes_meta.py.
 */
import type { CodeStage } from "../lib/codeStage";
import type { RunResult, Verdict } from "../types/result";

export type { Verdict };

export interface Display {
  title?: string | null;
  decision?: string | null;
  scores?: { ai_mean?: number | null; human_mean?: number | null } | null;
  tier?: string | null;
  topic?: string | null;
}

/** RunResult.summary (pipeline.summarise) plus the API's `code` field. */
export interface Summary {
  counts?: Partial<Record<Verdict, number>>;
  n_claims?: number;
  n_checklist_claims?: number;
  flagged_share?: number;
  main_flagged_share?: number;
  n_main?: number;
  checklist?: Record<string, number>;
  stability_mean?: number | null;
  adjudicated?: number;
  downgraded?: number;
  code?: { available: boolean; files: number };
}

export interface PaperRow {
  id: string;
  ref: string;
  source: string;
  external_id: string | null;
  title: string | null;
  pages: number | null;
  pdf_sha256: string | null;
  source_url: string | null;
  display: Display | null;
  analysis_id: string | null;
  summary: Summary | null;
  fingerprint?: string | null;
  onus_version?: string | null;
  runs?: number;
}


export interface AnalysisHeader {
  id: string;
  paper_id: string;
  mode: "batch" | "live";
  runs: number;
  fingerprint: string;
  onus_version: string;
  summary: Summary | null;
  created_at: string | null;
  status?: string;
  cost_eur?: number | null;
  finished_at?: string | null;
  error?: string | null;
}


export interface AnalysisResponse extends RunResult {
  display: Display | null;
  /** Live analyses only: the code-check stage that runs after the report is stored. */
  code_stage?: CodeStage | null;
  analysis: {
    id: string;
    paper_id: string;
    paper_ref: string;
    status: string;
    source_url: string | null;
    title: string | null;
  };
}

export interface Progress {
  stage: string;
  stages: string[];
  history: string[];
  info: Record<string, string | number>;
}

export interface AnalysisStatus {
  id: string;
  paper_id: string;
  status: "queued" | "running" | "done" | "failed";
  mode: string;
  progress: Progress | null;
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
}

export interface VersionRow {
  onus_version: string;
  fingerprint: string;
  mode: string;
  analyses: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface Meta {
  onus_version: string;
  fingerprint: string;
  models: Record<string, string>;
  featured: PaperRow[];
  featured_source: "config" | "auto";
  corpus_papers: number;
  has_findings: boolean;
  has_metrics: boolean;
  versions?: VersionRow[];
  eval_runs?: {
    count: number;
    max: number;
    runs: {
      run: string | null;
      started: string | null;
      commit: string | null;
      pipeline_fingerprint: string | null;
      harness_fingerprint: string | null;
    }[];
  };
  freeze?: {
    version: string | null;
    fingerprint: string;
    harness_fingerprint: string | null;
    created_at: string | null;
    matches_current: boolean;
  } | null;
  public_docs?: string[];
  live: {
    enabled: boolean;
    runs: number;
    code_checks: boolean;
    capabilities_probed_at?: string | null;
    max_pdf_mb: number;
    max_per_ip_per_hour: number;
    max_per_day: number;
    /** Set when live code checks are on. */
    code?: {
      max_zip_mb: number;
      max_reruns_per_day: number;
      max_reruns_per_ip_per_day: number;
      max_rerun_minutes: number;
      hosts: string[];
    } | null;
  };
}

export interface StartResponse {
  analysis_id: string;
  status: "queued" | "running" | "done";
  cached: boolean;
}
