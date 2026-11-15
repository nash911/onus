/**
 * Shapes returned by the Onus API around the RunResult contract. RunResult itself is generated from
 * schema/result.schema.json (`npm run gen:types` -> src/types/result.ts); the rest mirrors
 * onus/api/serialize.py, onus/eval/findings.py and onus/eval/metrics.py (merge-metrics).
 */
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
  fingerprint?: string;
  onus_version?: string;
  runs?: number;
}

export interface PapersPage {
  total: number;
  offset: number;
  limit: number;
  items: PaperRow[];
  facets: { decision: string[]; tier: string[]; verdict: Verdict[] };
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

export interface PaperDetail extends PaperRow {
  analyses: AnalysisHeader[];
}

export interface AnalysisResponse extends RunResult {
  display: Display | null;
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
  live: {
    enabled: boolean;
    runs: number;
    code_checks: boolean;
    max_pdf_mb: number;
    max_per_ip_per_hour: number;
    max_per_day: number;
  };
}

export interface StartResponse {
  analysis_id: string;
  status: "queued" | "running" | "done";
  cached: boolean;
}

// ---------------------------------------------------------------- findings.json (onus findings)

export type CI = [number, number] | null;

export interface Rq1Series {
  rho: number | null;
  ci95: CI;
  n: number;
  points: { x: number; y: number }[];
}

export interface Findings {
  papers?: number;
  splits?: Record<string, number>;
  bootstrap?: number;
  seed?: number;
  rq1?: { ai_score?: Rq1Series | null; human_score?: Rq1Series | null };
  rq2?: {
    difference: number | null;
    ci95: CI;
    mann_whitney_p: number | null;
    mean_accepted: number | null;
    mean_rejected: number | null;
    n_accepted: number;
    n_rejected: number;
    series?: { accepted: number[]; rejected: number[] };
  } | null;
  rq3?: {
    items: { key: string; yes: number; unbacked: number; contradicted?: number; unbacked_share: number | null }[];
  } | null;
  rq4?: {
    funnel: {
      papers: number;
      with_code: number;
      executable: number;
      rerun_attempted: number;
      consistent: number;
      note?: string;
    };
  } | null;
  _fixture?: string;
}

// ---------------------------------------------------------------- metrics.json (onus eval merge-metrics)

export interface Rate {
  k: number;
  n: number;
  rate: number | null;
  ci95: CI;
  papers?: number;
}

export interface RecallRow {
  attempted?: number;
  applied?: number;
  target_missing?: number;
  papers?: number;
  onus?: Rate | null;
  baseline?: Rate | null;
}

export interface StabilityMetrics {
  runs: number;
  na?: string;
  claims?: number;
  papers?: number;
  full_agreement?: Rate;
  mean_agreement?: number | null;
  matrix?: Record<string, Record<string, number>>;
}

export interface CodeAgreement {
  labelled?: number;
  excluded?: number;
  audit_classes?: Record<string, number>;
  na?: string;
  n?: number;
  accuracy?: number;
  kappa?: number | null;
  confusion?: {
    pred_mismatch_audit_mismatch: number;
    pred_mismatch_audit_passed: number;
    pred_ok_audit_mismatch: number;
    pred_ok_audit_passed: number;
  };
}

export interface SplitMetrics {
  fingerprint?: string;
  harness_fingerprint?: string;
  created_at?: string;
  mutation_plan?: string;
  runs?: number[];
  mutations?: { recall?: Record<string, RecallRow>; collateral?: { onus?: Rate | null; baseline?: Rate | null } };
  stability?: StabilityMetrics;
  code_agreement?: CodeAgreement;
  distributions?: { papers?: number; claims?: number; flagged?: Rate; verdicts?: Record<string, number> };
}

export interface Precision {
  correct: number;
  incorrect: number;
  unsure: number;
  precision: number | null;
  ci95: CI;
}

export interface Metrics {
  splits: Record<string, SplitMetrics>;
  pooled: {
    recall: Record<string, { onus: Rate | null; baseline: Rate | null }>;
    collateral: Rate | null;
  };
  labels: { systems: Record<string, Record<string, Precision>> } | null;
  eval_runs?: number;
  _fixture?: string;
}
