/**
 * Where claims and evidence sit in the PDF: GET /api/analyses/{id}/anchors (Session A, onus/api).
 *
 * Contract: rects are PDF points with a TOP-LEFT origin, [x0, y0, x1, y1], relative to the page box (index 0 of
 * `page_sizes` is page 1). pdf.js works in PDF user space (BOTTOM-LEFT origin, offset by the page's view box), so
 * a rect is first flipped into user space and then mapped by the page viewport, as pdf.js itself does
 * (pdf.js v6 removed `convertToViewportRectangle`; its two corners go through
 * `viewport.convertToViewportPoint`, which is what that method did). Self-contained (no imports) so `node --test` can load it.
 */

export type Rect = [number, number, number, number];

export interface Anchor {
  page: number;
  rects: Rect[];
  method?: string;
}

export interface Anchors {
  pdf: boolean;
  page_sizes: [number, number][];
  /** "<claim_id>", "<claim_id>:<ev_id>", "checklist:<key>:<ev_id>" */
  items: Record<string, Anchor[]>;
  missing: string[];
}

export const EMPTY_ANCHORS: Anchors = { pdf: false, page_sizes: [], items: {}, missing: [] };

/** The anchor key of a claim, or of one evidence item of a claim. */
export function anchorKey(claimId: string, evId?: string | null): string {
  return evId ? `${claimId}:${evId}` : claimId;
}

/** A checklist answer's evidence item. The answer itself is anchored through its checklist claim. */
export function checklistKey(key: string, evId: string): string {
  return `checklist:${key}:${evId}`;
}

/** The subset of pdf.js's PageViewport this module needs. */
export interface ViewportLike {
  viewBox: number[];
  convertToViewportPoint(x: number, y: number): number[];
}

/** Top-left-origin page rect -> PDF user space (bottom-left origin, view box offset). */
export function toUserSpace(r: Rect, viewBox: number[]): Rect {
  const [vx0 = 0, , , vy1 = 0] = viewBox;
  return [vx0 + r[0], vy1 - r[1], vx0 + r[2], vy1 - r[3]];
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** A contract rect as CSS pixels inside the rendered page (same scale and rotation as `vp`). */
export function toViewportBox(r: Rect, vp: ViewportLike): Box {
  const u = toUserSpace(r, vp.viewBox);
  const [a = 0, b = 0] = vp.convertToViewportPoint(u[0], u[1]);
  const [c = 0, d = 0] = vp.convertToViewportPoint(u[2], u[3]);
  return { left: Math.min(a, c), top: Math.min(b, d), width: Math.abs(c - a), height: Math.abs(d - b) };
}

/**
 * A viewport for a page that has not been loaded yet (rotation 0), built the way pdf.js builds one, so
 * placeholders and scroll targets agree with the rendered page.
 */
export function plainViewport(size: [number, number], scale: number): ViewportLike {
  const [w, h] = size;
  const viewBox = [0, 0, w, h];
  // pdf.js rotation 0: [scale, 0, 0, -scale, -scale * vx0, scale * vy1]
  const t = [scale, 0, 0, -scale, 0, scale * h];
  return {
    viewBox,
    convertToViewportPoint: (x, y) => [t[0]! * x + t[2]! * y + t[4]!, t[1]! * x + t[3]! * y + t[5]!],
  };
}

/** The first rect of an anchor list: where selecting the item scrolls to. */
export function firstRect(list: Anchor[] | undefined): { page: number; rect: Rect | null } | null {
  const a = list?.find((x) => x.page >= 1);
  if (!a) return null;
  return { page: a.page, rect: a.rects[0] ?? null };
}

/** Scroll offset that puts `y` (px from the top of the scroll content) about 30% down the visible pane. */
export function scrollTopFor(y: number, paneHeight: number, fraction = 0.3): number {
  return Math.max(0, Math.round(y - paneHeight * fraction));
}
