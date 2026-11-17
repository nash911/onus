/**
 * Product analytics: a small, failure-proof event queue (see src/backend/onus/api/analytics.py).
 *
 * No cookies and nothing that follows a visitor across visits: the session id is random per tab and lives in
 * sessionStorage. Nothing is sent when the browser asks not to be tracked (Do Not Track or Global Privacy
 * Control). Events are batched and sent to POST /api/events with sendBeacon (fetch keepalive as a fallback),
 * and every call is wrapped so tracking can never break the page.
 *
 * Self-contained (no DOM access at import time) so `node --test` can load it.
 */

export type EventType =
  | "page_view"
  | "submit"
  | "submit_result"
  | "example_click"
  | "progress_view"
  | "report_open"
  | "claim_open"
  | "evidence_click"
  | "margin_mark_click"
  | "tab_switch"
  | "filter"
  | "search"
  | "pdf_zoom"
  | "pdf_page"
  | "export"
  | "dispute_open"
  | "dispute_submit"
  | "outbound_click"
  | "heartbeat"
  | "client_error";

export type Props = Record<string, string | number | boolean | null | undefined>;

export interface QueuedEvent {
  type: EventType;
  /** milliseconds between the event and the send (set at flush); the server dates the event with it */
  ago?: number;
  path?: string;
  analysis_id?: string;
  props?: Props;
}

export interface Transport {
  /** Send one JSON body; `final` is true when the page is being hidden or closed. */
  send(body: string, final: boolean): void;
}

export interface TrackerOptions {
  enabled: boolean;
  session: string;
  transport: Transport;
  path?: () => string;
  now?: () => number;
  maxBatch?: number;
  maxQueue?: number;
}

export const ENDPOINT = "/api/events";

export class Tracker {
  private queue: (QueuedEvent & { at: number })[] = [];
  private analysisId: string | undefined;
  readonly enabled: boolean;
  readonly session: string;
  private readonly transport: Transport;
  private readonly path: () => string;
  private readonly now: () => number;
  private readonly maxBatch: number;
  private readonly maxQueue: number;

  constructor(o: TrackerOptions) {
    this.enabled = o.enabled;
    this.session = o.session;
    this.transport = o.transport;
    this.path = o.path ?? (() => "");
    this.now = o.now ?? (() => Date.now());
    this.maxBatch = o.maxBatch ?? 50;
    this.maxQueue = o.maxQueue ?? 500;
  }

  /** The report being viewed: attached to every event until cleared. */
  setAnalysis(id: string | undefined): void {
    this.analysisId = id || undefined;
  }

  track(type: EventType, props?: Props): void {
    if (!this.enabled) return;
    try {
      if (this.queue.length >= this.maxQueue) return;
      const clean: Props = {};
      for (const [k, v] of Object.entries(props ?? {})) {
        if (v === undefined || v === null) continue;
        clean[k] = typeof v === "string" ? v.slice(0, 120) : v;
      }
      this.queue.push({
        at: this.now(),
        type,
        path: this.path().slice(0, 255) || undefined,
        analysis_id: this.analysisId,
        props: Object.keys(clean).length ? clean : undefined,
      });
    } catch {
      /* never throw from tracking */
    }
  }

  pending(): number {
    return this.queue.length;
  }

  /** Send queued events in batches; returns how many were handed to the transport. */
  flush(final = false): number {
    if (!this.enabled || !this.queue.length) return 0;
    let sent = 0;
    try {
      while (this.queue.length) {
        const t = this.now();
        const batch = this.queue.splice(0, this.maxBatch).map(({ at, ...e }) => ({ ...e, ago: Math.max(0, t - at) }));
        this.transport.send(JSON.stringify({ session: this.session, events: batch }), final);
        sent += batch.length;
      }
    } catch {
      this.queue = [];
    }
    return sent;
  }
}

/** Do Not Track or Global Privacy Control set: send nothing. */
export function optedOut(nav: { doNotTrack?: string | null; globalPrivacyControl?: boolean } | undefined): boolean {
  if (!nav) return true;
  return nav.doNotTrack === "1" || nav.doNotTrack === "yes" || nav.globalPrivacyControl === true;
}

export function newSessionId(rand: () => number = Math.random): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (ch) => {
    const r = (rand() * 16) | 0;
    return (ch === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/** Screen width bucket, not the exact size. */
export function screenBucket(width: number): string {
  if (width < 640) return "phone";
  if (width < 1024) return "tablet";
  if (width < 1600) return "laptop";
  return "desktop";
}

/** The referrer's host when it is another site, else undefined. */
export function referrerHost(referrer: string, ownHost: string): string | undefined {
  try {
    if (!referrer) return undefined;
    const h = new URL(referrer).hostname;
    return h && h !== ownHost ? h : undefined;
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------- the browser singleton

const NOOP = new Tracker({ enabled: false, session: "none", transport: { send: () => undefined } });
let current: Tracker = NOOP;
let installed = false;
let analysisId: string | undefined; // kept here too: a page may set it before tracking is installed

function browserTransport(): Transport {
  return {
    send(body, final) {
      const nav = navigator as Navigator & { sendBeacon?: (u: string, d: BodyInit) => boolean };
      const blob = new Blob([body], { type: "application/json" });
      if (nav.sendBeacon && nav.sendBeacon(ENDPOINT, blob)) return;
      void fetch(ENDPOINT, { method: "POST", body, headers: { "Content-Type": "application/json" }, keepalive: final }).catch(
        () => undefined,
      );
    },
  };
}

function sessionId(): string {
  try {
    const k = "onus.session";
    const old = sessionStorage.getItem(k);
    if (old && /^[A-Za-z0-9-]{8,36}$/.test(old)) return old;
    const id = newSessionId();
    sessionStorage.setItem(k, id);
    return id;
  } catch {
    return newSessionId();
  }
}

/** Start tracking in the browser: flush timer, flush on hide, heartbeat while visible, JS errors. Idempotent. */
export function installTracking(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  try {
    const nav = navigator as Navigator & { globalPrivacyControl?: boolean };
    current = new Tracker({
      enabled: !optedOut(nav),
      session: sessionId(),
      transport: browserTransport(),
      path: () => window.location.pathname,
    });
    current.setAnalysis(analysisId);
    if (!current.enabled) return;
    window.setInterval(() => current.flush(false), 5000);
    const HEARTBEAT_S = 15;
    window.setInterval(() => {
      if (document.visibilityState === "visible") current.track("heartbeat", { seconds: HEARTBEAT_S });
    }, HEARTBEAT_S * 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") current.flush(true);
    });
    window.addEventListener("pagehide", () => current.flush(true));
    window.addEventListener("error", (e) => {
      const where = e.filename ? `${e.filename.split("/").pop()}:${e.lineno}` : "";
      current.track("client_error", { message: String(e.message ?? "error"), where });
    });
    window.addEventListener("unhandledrejection", (e) => {
      const r = (e as PromiseRejectionEvent).reason;
      current.track("client_error", { message: String(r?.message ?? r ?? "rejection"), where: "promise" });
    });
  } catch {
    current = NOOP;
  }
}

export function track(type: EventType, props?: Props): void {
  current.track(type, props);
}

export function setTrackedAnalysis(id: string | undefined): void {
  analysisId = id || undefined;
  current.setAnalysis(analysisId);
}
