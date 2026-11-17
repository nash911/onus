/**
 * Verdict and checklist-status styling. Every verdict is shown with colour, label and icon together
 * (never colour alone): supported green, overstated burnt orange, untraced violet, contradicted vermilion, not checkable grey
 * (the "Journal" brand colours, defined in index.css).
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
  /** badge classes: text, background and border colour */
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
    badge: "text-v-supported bg-v-supported-bg border-v-supported-line",
    fill: "#067647",
    description: "Evidence found and as strong as the claim's wording.",
  },
  overstated: {
    label: "Overstated",
    Icon: IconOverstated,
    badge: "text-v-overstated bg-v-overstated-bg border-v-overstated-line",
    fill: "#b54708",
    description: "Evidence found but weaker than the wording.",
  },
  untraced: {
    label: "Untraced",
    Icon: IconUntraced,
    badge: "text-v-untraced bg-v-untraced-bg border-v-untraced-line",
    fill: "#6941c6",
    description: "No evidence located in the paper or its code.",
  },
  contradicted: {
    label: "Contradicted",
    Icon: IconContradicted,
    badge: "text-v-contradicted bg-v-contradicted-bg border-v-contradicted-line",
    fill: "#b42318",
    description: "The paper's own evidence disagrees with the claim.",
  },
  not_checkable: {
    label: "Not checkable",
    Icon: IconNotCheckable,
    badge: "text-v-notcheckable bg-v-notcheckable-bg border-v-notcheckable-line",
    fill: "#667085",
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
    badge: "text-slate-600 bg-white border-slate-300",
    fill: "#d4d4d8",
    description: "Answered No or NA; nothing to back.",
  },
  not_checkable: { ...VERDICTS.not_checkable, description: "Could not be checked from the paper." },
};

export function isVerdict(v: string): v is Verdict {
  return v in VERDICTS;
}
