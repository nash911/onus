/**
 * Verdict and checklist-status styling. Every verdict is shown with colour, label and icon together
 * (never colour alone): supported green, overstated amber, untraced slate, contradicted red, not checkable grey.
 */
import type { ComponentType, SVGProps } from "react";
import {
  IconContradicted,
  IconMinus,
  IconNotCheckable,
  IconOverstated,
  IconSupported,
  IconUntraced,
} from "../components/Icons";
import type { Verdict } from "../types/result";

export interface Style {
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** badge classes: text, background and ring */
  badge: string;
  /** solid colour for bars and chart marks */
  fill: string;
  description: string;
}

/** Risk order: contradicted 4 · overstated 3 · untraced 2 · not_checkable 1 · supported 0 */
export const VERDICT_ORDER: Verdict[] = ["contradicted", "overstated", "untraced", "not_checkable", "supported"];
export const FLAGGED: Verdict[] = ["contradicted", "overstated", "untraced"];

export const VERDICTS: Record<Verdict, Style> = {
  supported: {
    label: "Supported",
    Icon: IconSupported,
    badge: "text-green-800 bg-green-50 ring-green-700/30",
    fill: "#15803d",
    description: "Evidence found and as strong as the claim's wording.",
  },
  overstated: {
    label: "Overstated",
    Icon: IconOverstated,
    badge: "text-amber-900 bg-amber-50 ring-amber-700/40",
    fill: "#d97706",
    description: "Evidence found but weaker than the wording.",
  },
  untraced: {
    label: "Untraced",
    Icon: IconUntraced,
    badge: "text-slate-800 bg-slate-100 ring-slate-600/40",
    fill: "#475569",
    description: "No evidence located in the paper or its code.",
  },
  contradicted: {
    label: "Contradicted",
    Icon: IconContradicted,
    badge: "text-red-800 bg-red-50 ring-red-700/30",
    fill: "#b91c1c",
    description: "The paper's own evidence disagrees with the claim.",
  },
  not_checkable: {
    label: "Not checkable",
    Icon: IconNotCheckable,
    badge: "text-zinc-700 bg-zinc-100 ring-zinc-500/30",
    fill: "#a1a1aa",
    description: "Needs something Onus cannot access or parse, with the reason.",
  },
};

export type AuditStatus = "backed" | "unbacked" | "contradicted" | "not_applicable" | "not_checkable";

export const AUDIT_STATUS: Record<AuditStatus, Style> = {
  backed: { ...VERDICTS.supported, label: "Backed", description: "The paper shows what the answer claims." },
  unbacked: { ...VERDICTS.untraced, label: "Unbacked", description: "Nothing in the paper backs the answer." },
  contradicted: { ...VERDICTS.contradicted, label: "Contradicted", description: "The paper contradicts the answer." },
  not_applicable: {
    label: "Not applicable",
    Icon: IconMinus,
    badge: "text-zinc-600 bg-white ring-zinc-400/40",
    fill: "#d4d4d8",
    description: "Answered No or NA; nothing to back.",
  },
  not_checkable: { ...VERDICTS.not_checkable, description: "Could not be checked from the paper." },
};

export function isVerdict(v: string): v is Verdict {
  return v in VERDICTS;
}
