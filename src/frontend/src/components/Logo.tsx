/**
 * The Onus logo ("Citation"): a ring held in square brackets, like a citation mark, beside the serif wordmark.
 * Ink brackets, link-blue ring (blue, so the mark never reads as one of the verdict colours).
 * public/favicon.svg is the same mark on a paper tile.
 */

const INK = "#1B2333";
const RING = "#1F4E8C";

export function LogoMark({ size = 24, className }: { size?: number; className?: string }) {
  // Heavier strokes at small sizes keep the brackets and ring apart.
  const small = size < 32;
  return (
    <svg width={size} height={size} viewBox="0 0 200 200" aria-hidden="true" focusable="false" className={className}>
      <path d={small ? "M66 28 H32 V172 H66" : "M64 30 H34 V170 H64"} fill="none" stroke={INK} strokeWidth={small ? 24 : 16} />
      <path d={small ? "M134 28 H168 V172 H134" : "M136 30 H166 V170 H136"} fill="none" stroke={INK} strokeWidth={small ? 24 : 16} />
      <circle cx="100" cy="100" r={small ? 30 : 32} fill="none" stroke={RING} strokeWidth={small ? 26 : 18} />
    </svg>
  );
}

/** Mark plus wordmark. The text is real text, so the link reads "onus" to screen readers. */
export function Logo({ size = 26 }: { size?: number }) {
  return (
    <span className="inline-flex items-center gap-2">
      <LogoMark size={Math.round(size * 1.15)} />
      <span className="font-serif font-semibold leading-none tracking-tight text-slate-900" style={{ fontSize: Math.round(size * 0.95) }}>
        onus
      </span>
    </span>
  );
}
