/** Small inline SVG icons (decorative: every use sits next to a visible text label). */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement>;

function Svg({ children, ...p }: P) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...p}
    >
      {children}
    </svg>
  );
}

/** supported: a check in a circle */
export const IconSupported = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M5.2 8.2l1.9 1.9 3.7-4" />
  </Svg>
);

/** overstated: an upward arrow inside a triangle */
export const IconOverstated = (p: P) => (
  <Svg {...p}>
    <path d="M8 1.8l6.4 11.4H1.6z" />
    <path d="M8 11V6.4M6.3 8L8 6.3 9.7 8" />
  </Svg>
);

/** untraced: a question mark in a dashed circle */
export const IconUntraced = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" strokeDasharray="2.2 1.8" />
    <path d="M6.3 6.3a1.8 1.8 0 113 1.4c-.7.4-1.3.8-1.3 1.6" />
    <circle cx="8" cy="11.3" r=".35" fill="currentColor" />
  </Svg>
);

/** contradicted: a cross in a square */
export const IconContradicted = (p: P) => (
  <Svg {...p}>
    <rect x="1.9" y="1.9" width="12.2" height="12.2" rx="2" />
    <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />
  </Svg>
);

/** not checkable: a slashed circle */
export const IconNotCheckable = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M3.6 12.4l8.8-8.8" />
  </Svg>
);

export const IconMinus = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M5 8h6" />
  </Svg>
);

export const IconClose = (p: P) => (
  <Svg {...p}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Svg>
);

export const IconExternal = (p: P) => (
  <Svg {...p}>
    <path d="M9.5 2.5h4v4M13.5 2.5L7.5 8.5M12 9.5v3.5a.5.5 0 01-.5.5h-8a.5.5 0 01-.5-.5v-8a.5.5 0 01.5-.5H7" />
  </Svg>
);

export const IconDownload = (p: P) => (
  <Svg {...p}>
    <path d="M8 2v8.5M4.8 7.5L8 10.7l3.2-3.2M2.5 13.5h11" />
  </Svg>
);

export const IconDot = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="3" fill="currentColor" stroke="none" />
  </Svg>
);

export const IconSpinner = (p: P) => (
  <Svg {...p} className={`animate-spin ${p.className ?? ""}`}>
    <path d="M8 1.8a6.2 6.2 0 106.2 6.2" />
  </Svg>
);

export const IconCircle = (p: P) => (
  <Svg {...p}>
    <circle cx="8" cy="8" r="6.25" />
  </Svg>
);
