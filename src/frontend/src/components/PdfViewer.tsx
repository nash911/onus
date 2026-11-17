/**
 * The paper, rendered with pdf.js (pdfjs-dist, worker bundled by Vite; nothing loads from a CDN).
 *
 * Pages render lazily: an IntersectionObserver tracks the visible pages and only those ±1 get a canvas and a
 * text layer (so text can be selected and copied); the rest are placeholders of the right size. An overlay per
 * page draws the selected claim (verdict fill + outline), the selected evidence (dashed, neutral) and one small
 * verdict mark per claim in a column at the page's left edge, level with its first line. Rect maths is in lib/anchors.ts.
 */
import { GlobalWorkerOptions, TextLayer, getDocument, type PDFDocumentProxy, type PDFPageProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "./pdfTextLayer.css";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { plainViewport, scrollTopFor, toViewportBox, type Rect, type ViewportLike } from "../lib/anchors";
import { VERDICTS } from "../lib/verdicts";
import type { Verdict } from "../types/result";
import { IconSpinner } from "./Icons";

GlobalWorkerOptions.workerSrc = workerUrl;

export interface PdfMark {
  claimId: string;
  verdict: Verdict;
  page: number;
  rect: Rect;
  /** Verdict and the start of the quote, for the label and the hover text. */
  label: string;
}

export interface PdfHighlight {
  page: number;
  rects: Rect[];
}

export interface ScrollRequest {
  page: number;
  rect: Rect | null;
  /** Bumped for every request, so selecting the same item again scrolls again. */
  seq: number;
}

export interface PdfViewerProps {
  url: string;
  /** From /anchors; used for placeholder sizes before pages load. */
  pageSizes?: [number, number][];
  marks: PdfMark[];
  selected: { verdict: Verdict; highlights: PdfHighlight[] } | null;
  evidence: PdfHighlight[] | null;
  scroll: ScrollRequest | null;
  onMark: (claimId: string) => void;
  /** The PDF does not exist (404) or cannot be read. */
  onUnavailable: (why: "missing" | "error") => void;
  /** False while the pane is hidden (narrow layout, Report tab): scrolling waits until it is shown. */
  active?: boolean;
}

const GAP = 12; // px between pages
const PAD = 16; // px around the page column
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;

const reduceMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export default function PdfViewer(props: PdfViewerProps) {
  const { url, pageSizes, onUnavailable, active = true } = props;
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [sizes, setSizes] = useState<[number, number][]>([]);
  const [zoom, setZoom] = useState<number | "fit">("fit");
  const [width, setWidth] = useState(0);
  const [visible, setVisible] = useState<Set<number>>(new Set([1]));
  const [current, setCurrent] = useState(1);
  const scroller = useRef<HTMLDivElement>(null);
  const pageEls = useRef<(HTMLDivElement | null)[]>([]);
  const unavailable = useRef(onUnavailable);
  unavailable.current = onUnavailable;

  // ---- load the document
  useEffect(() => {
    let dead = false;
    const task = getDocument({ url });
    task.promise.then(
      async (d) => {
        if (dead) return;
        const first = (await d.getPage(1)).getViewport({ scale: 1 });
        const fallback: [number, number] = [first.width, first.height];
        const known = pageSizes && pageSizes.length === d.numPages ? pageSizes : null;
        setSizes(Array.from({ length: d.numPages }, (_, i) => known?.[i] ?? fallback));
        setDoc(d);
      },
      (e: unknown) => {
        if (dead) return;
        const status = (e as { status?: number })?.status;
        unavailable.current(status === 404 ? "missing" : "error");
      },
    );
    return () => {
      dead = true;
      void task.destroy();
    };
    // pageSizes only seeds placeholders; a new array must not reload the document
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  // ---- fit width
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const maxW = sizes.reduce((m, s) => Math.max(m, s[0]), 0) || 612;
  const fit = width > 0 ? Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, (width - 2 * PAD) / maxW)) : 1;
  const scale = zoom === "fit" ? fit : zoom;

  // ---- which pages are on screen
  useEffect(() => {
    const root = scroller.current;
    if (!root || !sizes.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          const next = new Set(prev);
          for (const e of entries) {
            const n = Number((e.target as HTMLElement).dataset.page);
            if (e.isIntersecting) next.add(n);
            else next.delete(n);
          }
          return next;
        });
      },
      { root, rootMargin: "100px 0px" },
    );
    pageEls.current.forEach((el) => el && io.observe(el));
    return () => io.disconnect();
  }, [sizes.length]);

  const near = useMemo(() => {
    const s = new Set<number>();
    for (const n of visible) for (const k of [n - 1, n, n + 1]) if (k >= 1 && k <= sizes.length) s.add(k);
    return s;
  }, [visible, sizes.length]);

  // ---- page offsets (for scrolling and "page N of M"), from the layout itself
  const offsets = useMemo(() => {
    const out: number[] = [];
    let y = PAD;
    for (const [, h] of sizes) {
      out.push(y);
      y += Math.floor(h * scale) + GAP;
    }
    return out;
  }, [sizes, scale]);

  const onScroll = useCallback(() => {
    const el = scroller.current;
    if (!el || !offsets.length) return;
    const probe = el.scrollTop + el.clientHeight * 0.3;
    let n = 1;
    for (let i = 0; i < offsets.length; i++) if (offsets[i]! <= probe) n = i + 1;
    setCurrent(n);
  }, [offsets]);

  // ---- scroll requests: the target rect about 30% from the top
  const pending = useRef<ScrollRequest | null>(null);
  useEffect(() => {
    if (props.scroll) pending.current = props.scroll;
  }, [props.scroll]);
  useEffect(() => {
    const req = pending.current;
    const el = scroller.current;
    if (!req || !el || !active || !sizes.length || el.clientHeight === 0) return;
    // A pane that was hidden still has its old width in state until the ResizeObserver reports; scrolling now
    // would use the old scale. Take the width and let the new scale re-run this effect.
    if (el.clientWidth !== width) {
      setWidth(el.clientWidth);
      return;
    }
    const size = sizes[req.page - 1];
    if (!size) return;
    const box = req.rect ? toViewportBox(req.rect, plainViewport(size, scale)) : { top: 0 };
    const y = offsets[req.page - 1]! + box.top;
    el.scrollTo({ top: scrollTopFor(y, el.clientHeight), behavior: reduceMotion() ? "auto" : "smooth" });
    pending.current = null;
  }, [props.scroll, active, sizes, scale, offsets, width]);

  // ---- marks and highlights by page
  const byPage = useMemo(() => {
    const m = new Map<number, { marks: PdfMark[]; sel: Rect[]; ev: Rect[] }>();
    const get = (p: number) => {
      let v = m.get(p);
      if (!v) m.set(p, (v = { marks: [], sel: [], ev: [] }));
      return v;
    };
    for (const k of props.marks) get(k.page).marks.push(k);
    for (const h of props.selected?.highlights ?? []) get(h.page).sel.push(...h.rects);
    for (const h of props.evidence ?? []) get(h.page).ev.push(...h.rects);
    return m;
  }, [props.marks, props.selected, props.evidence]);

  const zoomBy = (f: number) =>
    setZoom(Math.round(Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, scale * f)) * 100) / 100);

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-100">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-white px-3 py-1.5 text-sm" role="toolbar" aria-label="Paper view">
        <button type="button" onClick={() => setZoom("fit")} aria-pressed={zoom === "fit"} className={`rounded px-2 py-1 ${zoom === "fit" ? "bg-slate-200 font-medium" : "hover:bg-slate-100"}`}>
          Fit width
        </button>
        <button type="button" onClick={() => zoomBy(1 / 1.2)} aria-label="Zoom out" className="h-7 w-7 rounded hover:bg-slate-100">
          −
        </button>
        <span className="w-12 text-center tabular-nums text-slate-600" aria-live="polite">
          {Math.round(scale * 100)}%
        </span>
        <button type="button" onClick={() => zoomBy(1.2)} aria-label="Zoom in" className="h-7 w-7 rounded hover:bg-slate-100">
          +
        </button>
        <span className="ml-auto tabular-nums text-slate-600">
          {sizes.length ? `Page ${current} of ${sizes.length}` : ""}
        </span>
      </div>
      <div ref={scroller} onScroll={onScroll} className="relative min-h-0 flex-1 overflow-auto" style={{ padding: PAD }}>
        {!doc && (
          <p className="flex items-center gap-2 p-4 text-sm text-slate-600" role="status">
            <IconSpinner /> Loading the paper…
          </p>
        )}
        {doc &&
          sizes.map((size, i) => {
            const n = i + 1;
            const extra = byPage.get(n);
            return (
              <PdfPage
                key={n}
                ref={(el) => {
                  pageEls.current[i] = el;
                }}
                doc={doc}
                n={n}
                size={size}
                scale={scale}
                render={near.has(n)}
                marks={extra?.marks}
                selected={extra?.sel}
                selectedVerdict={props.selected?.verdict}
                evidence={extra?.ev}
                onMark={props.onMark}
              />
            );
          })}
      </div>
    </div>
  );
}

interface PageProps {
  doc: PDFDocumentProxy;
  n: number;
  size: [number, number];
  scale: number;
  render: boolean;
  marks?: PdfMark[];
  selected?: Rect[];
  selectedVerdict?: Verdict;
  evidence?: Rect[];
  onMark: (claimId: string) => void;
  ref?: React.Ref<HTMLDivElement>;
}

const MARK = 14;
const MARK_GAP = 3;

/** Marks sit in a column at the page's left edge, at the height of the claim's first line; claims that start on
 * the same line go side by side instead of on top of each other. */
function placeMarks(marks: PdfMark[], view: ViewportLike): { m: PdfMark; top: number; left: number }[] {
  const placed = marks
    .map((m) => {
      const b = toViewportBox(m.rect, view);
      return { m, top: b.top + Math.max(0, (b.height - MARK) / 2), x: b.left, left: 0 };
    })
    .sort((a, b) => a.top - b.top || a.x - b.x);
  let rowTop = -Infinity;
  let col = 0;
  for (const p of placed) {
    if (p.top - rowTop < MARK) col += 1;
    else {
      rowTop = p.top;
      col = 0;
    }
    p.top = col ? rowTop : p.top;
    p.left = 4 + col * (MARK + MARK_GAP);
  }
  return placed;
}

const PdfPage = memo(function PdfPage({ doc, n, size, scale, render, marks, selected, selectedVerdict, evidence, onMark, ref }: PageProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const text = useRef<HTMLDivElement>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [drawn, setDrawn] = useState(false);

  useEffect(() => {
    if (!render || page) return;
    let dead = false;
    doc.getPage(n).then((p) => !dead && setPage(p), () => undefined);
    return () => {
      dead = true;
    };
  }, [render, page, doc, n]);

  const vp = useMemo(() => (page ? page.getViewport({ scale }) : null), [page, scale]);
  const w = Math.floor(vp ? vp.width : size[0] * scale);
  const h = Math.floor(vp ? vp.height : size[1] * scale);

  useEffect(() => {
    const c = canvas.current;
    const t = text.current;
    if (!render || !page || !vp || !c || !t) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = Math.floor(vp.width * dpr);
    c.height = Math.floor(vp.height * dpr);
    const task = page.render({ canvas: c, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined });
    t.replaceChildren();
    const layer = new TextLayer({ textContentSource: page.streamTextContent(), container: t, viewport: vp });
    task.promise.then(() => setDrawn(true), () => undefined);
    layer.render().catch(() => undefined);
    return () => {
      task.cancel();
      layer.cancel();
      setDrawn(false);
    };
  }, [render, page, vp]);

  const view: ViewportLike = vp ?? plainViewport(size, scale);
  const fill = selectedVerdict ? VERDICTS[selectedVerdict].fill : "#1d4ed8";

  return (
    <div
      ref={ref}
      data-page={n}
      className="relative mx-auto bg-white shadow-sm ring-1 ring-slate-300"
      style={{ width: w, height: h, marginBottom: GAP, ["--scale-factor" as string]: scale, ["--total-scale-factor" as string]: scale }}
      aria-label={`Page ${n}`}
      role="region"
    >
      {render && <canvas ref={canvas} className="absolute inset-0" style={{ width: w, height: h }} aria-hidden="true" />}
      {render && <div ref={text} className="textLayer" />}
      {(!render || !drawn) && (
        <span className="absolute inset-0 grid place-items-center text-xs text-slate-400" aria-hidden="true">
          {n}
        </span>
      )}
      <div className="pointer-events-none absolute inset-0" style={{ zIndex: 2 }}>
        {(evidence ?? []).map((r, i) => {
          const b = toViewportBox(r, view);
          return (
            <div
              key={`e${i}`}
              className="absolute rounded-sm"
              style={{ left: b.left - 2, top: b.top - 1, width: b.width + 4, height: b.height + 2, background: "rgb(100 116 139 / 0.14)", outline: "2px dashed #334155", outlineOffset: 1 }}
            />
          );
        })}
        {(selected ?? []).map((r, i) => {
          const b = toViewportBox(r, view);
          return (
            <div
              key={`s${i}`}
              className="absolute rounded-sm"
              style={{ left: b.left - 2, top: b.top - 1, width: b.width + 4, height: b.height + 2, background: `${fill}33`, outline: `2px solid ${fill}` }}
            />
          );
        })}
        {placeMarks(marks ?? [], view).map(({ m, top, left }) => {
          const v = VERDICTS[m.verdict];
          return (
            <button
              key={m.claimId}
              type="button"
              onClick={() => onMark(m.claimId)}
              aria-label={m.label}
              title={m.label}
              className="pointer-events-auto absolute grid h-3.5 w-3.5 place-items-center rounded-full ring-2 ring-white hover:scale-125 focus-visible:scale-125"
              style={{ left, top, background: v.fill }}
            >
              <v.Icon className="h-2.5 w-2.5 text-white" aria-hidden="true" />
            </button>
          );
        })}
      </div>
    </div>
  );
});
