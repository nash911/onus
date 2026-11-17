// Phase 13b: the two-page app. Rect transform, filters and search, URL selection, Markdown export, redirects.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { anchorKey, firstRect, plainViewport, scrollTopFor, toUserSpace, toViewportBox, type Anchors, type Rect } from "../src/lib/anchors.ts";
import {
  DEFAULT_FILTER,
  filterClaims,
  readSelection,
  reportMarkdown,
  stepClaim,
  toggleVerdict,
  writeSelection,
} from "../src/lib/reportView.ts";
import { redirectFor } from "../src/lib/routes.ts";
import type { Claim, RunResult } from "../src/types/result.ts";

const fixture = (name: string) =>
  JSON.parse(readFileSync(new URL(`../../../tests/fixtures/${name}`, import.meta.url), "utf8"));
const report = fixture("report_sample.json") as RunResult;
const anchors = fixture("anchors_sample.json") as Anchors;

const close = (a: number, b: number, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

// ---------------------------------------------------------------- rect transform

/** A one-page PDF with the given MediaBox (and optional /Rotate), with a correct xref table. */
function tinyPdf(mediaBox: number[], rotate = 0): Uint8Array {
  const objs = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    `<< /Type /Page /Parent 2 0 R /MediaBox [${mediaBox.join(" ")}] /Rotate ${rotate} >>`,
  ];
  let body = "%PDF-1.4\n";
  const offsets: number[] = [];
  objs.forEach((o, i) => {
    offsets.push(body.length);
    body += `${i + 1} 0 obj\n${o}\nendobj\n`;
  });
  const xref = body.length;
  body += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n`;
  for (const off of offsets) body += `${String(off).padStart(10, "0")} 00000 n \n`;
  body += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return new TextEncoder().encode(body);
}

async function pdfjsViewport(mediaBox: number[], scale: number) {
  const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
  const task = pdfjs.getDocument({ data: tinyPdf(mediaBox), verbosity: 0 });
  const doc = await task.promise;
  const page = await doc.getPage(1);
  return { vp: page.getViewport({ scale }), rotate: page.rotate, done: () => task.destroy() };
}

const R: Rect = [72, 100, 300, 112]; // a line of text 100 pt below the top, 72 pt in from the left

for (const scale of [1, 1.75]) {
  test(`rect transform matches pdf.js at zoom ${scale} (rotation 0)`, async () => {
    const { vp, rotate, done } = await pdfjsViewport([0, 0, 612, 792], scale);
    assert.equal(rotate, 0);
    const box = toViewportBox(R, vp);
    // top-left origin in, top-left CSS pixels out: just scaled
    close(box.left, 72 * scale);
    close(box.top, 100 * scale);
    close(box.width, 228 * scale);
    close(box.height, 12 * scale);
    // and the stand-in viewport for unloaded pages agrees with pdf.js
    const plain = toViewportBox(R, plainViewport([612, 792], scale));
    for (const k of ["left", "top", "width", "height"] as const) close(plain[k], box[k]);
    await done();
  });
}

test("rect transform with a page box that does not start at 0,0", async () => {
  // MediaBox [20 30 632 822]: same 612 x 792 page, offset. Rects are relative to the page box, so the result
  // must equal the unshifted page.
  const { vp, done } = await pdfjsViewport([20, 30, 632, 822], 2);
  assert.deepEqual(toUserSpace(R, vp.viewBox), [92, 722, 320, 710]);
  const box = toViewportBox(R, vp);
  close(box.left, 144);
  close(box.top, 200);
  close(box.width, 456);
  close(box.height, 24);
  await done();
});

test("fixture anchors fall inside their pages", () => {
  assert.equal(anchors.pdf, true);
  for (const [key, list] of Object.entries(anchors.items)) {
    for (const a of list) {
      const [w, h] = anchors.page_sizes[a.page - 1]!;
      for (const r of a.rects) assert.ok(r[0] >= 0 && r[1] >= 0 && r[2] <= w && r[3] <= h && r[0] < r[2] && r[1] < r[3], key);
    }
  }
  const c = report.claims[0]!;
  assert.ok(anchors.items[anchorKey(c.id)], "every fixture claim is anchored");
  assert.equal(firstRect(anchors.items[anchorKey(c.id)])!.page, anchors.items[c.id]![0]!.page);
  assert.equal(firstRect(undefined), null);
  assert.equal(scrollTopFor(1000, 800), 760);
  assert.equal(scrollTopFor(100, 800), 0);
});

// ---------------------------------------------------------------- filters and search

const claims = report.claims as Claim[];

test("claims are listed in paper order, all verdicts on by default", () => {
  const shown = filterClaims(claims, DEFAULT_FILTER);
  assert.equal(shown.length, claims.length);
  for (let i = 1; i < shown.length; i++) {
    const a = shown[i - 1]!.location;
    const b = shown[i]!.location;
    assert.ok(a.page < b.page || (a.page === b.page && a.start <= b.start));
  }
});

test("verdict toggles, main-only and search combine", () => {
  const off = toggleVerdict(DEFAULT_FILTER.verdicts, "supported");
  const noSupported = filterClaims(claims, { ...DEFAULT_FILTER, verdicts: off });
  assert.ok(noSupported.length < claims.length);
  assert.ok(noSupported.every((c) => c.verdict !== "supported"));
  assert.ok(toggleVerdict(off, "supported").has("supported"));

  const main = filterClaims(claims, { ...DEFAULT_FILTER, mainOnly: true });
  assert.ok(main.length > 0 && main.every((c) => c.is_main));

  const hit = filterClaims(claims, { ...DEFAULT_FILTER, text: "  HUMANITY’S   last " });
  assert.ok(hit.length > 0 && hit.every((c) => /humanity’s/i.test(c.quote) && /last/i.test(c.quote)));
  assert.equal(filterClaims(claims, { ...DEFAULT_FILTER, text: "zzzz-no-such-words" }).length, 0);
  assert.equal(filterClaims(claims, { ...DEFAULT_FILTER, verdicts: new Set() }).length, 0);
});

test("j / k stepping", () => {
  const shown = filterClaims(claims, DEFAULT_FILTER);
  assert.equal(stepClaim(shown, null, 1), shown[0]!.id);
  assert.equal(stepClaim(shown, null, -1), shown.at(-1)!.id);
  assert.equal(stepClaim(shown, shown[0]!.id, 1), shown[1]!.id);
  assert.equal(stepClaim(shown, shown[0]!.id, -1), shown[0]!.id);
  assert.equal(stepClaim([], null, 1), null);
});

// ---------------------------------------------------------------- URL selection

test("selection round-trips through the URL", () => {
  const p = writeSelection(new URLSearchParams("x=1"), { claim: "abc", ev: "ev3" });
  assert.equal(p.toString(), "x=1&claim=abc&ev=ev3");
  assert.deepEqual(readSelection(p), { claim: "abc", ev: "ev3", tab: "claims" });
  // a new claim drops the evidence; a tab other than claims is written
  const q = writeSelection(p, { claim: "def" });
  assert.deepEqual(readSelection(q), { claim: "def", ev: null, tab: "claims" });
  const t = writeSelection(q, { tab: "code" });
  assert.equal(t.get("tab"), "code");
  assert.deepEqual(readSelection(new URLSearchParams(t.toString())), readSelection(t));
  // clearing
  const none = writeSelection(t, { claim: null, tab: "claims" });
  assert.equal(none.toString(), "x=1");
  // junk is ignored: unknown tab, ev without a claim
  assert.deepEqual(readSelection(new URLSearchParams("tab=json&ev=ev1")), { claim: null, ev: null, tab: "claims" });
});

// ---------------------------------------------------------------- Markdown export

test("Markdown export: title, counts, flagged claims, not checked", () => {
  const md = reportMarkdown(report, "AI Passes Humanity’s Last Exam", "https://example.org/p");
  const lines = md.split("\n");
  assert.equal(lines[0], "# AI Passes Humanity’s Last Exam");
  assert.ok(md.includes("Source: https://example.org/p"));
  for (const v of ["Supported", "Overstated", "Untraced", "Contradicted", "Not checkable"]) {
    assert.match(md, new RegExp(`^- ${v}: \\d+$`, "m"));
  }
  const sum = [...md.matchAll(/^- [A-Z][a-z ]+: (\d+)$/gm)].reduce((s, m) => s + Number(m[1]), 0);
  assert.equal(sum, claims.length);
  const flagged = claims.filter((c) => ["contradicted", "overstated", "untraced"].includes(c.verdict));
  assert.equal((md.match(/^### \d+\. /gm) ?? []).length, flagged.length);
  const first = filterClaims(flagged, DEFAULT_FILTER)[0]!;
  assert.ok(md.includes(`> ${first.quote.replace(/\s+/g, " ").trim()}`));
  assert.ok(md.includes(`(p. ${first.location.page})`));
  assert.ok(md.includes(first.reason.replace(/\s+/g, " ").trim()));
  assert.ok(md.includes("## Not checked"));
  for (const n of report.not_checked ?? []) assert.ok(md.includes(n.what.replace(/\s+/g, " ").trim()));
  // supported claims are not listed one by one
  const sup = claims.find((c) => c.verdict === "supported");
  if (sup) assert.ok(!md.includes(`> ${sup.quote.replace(/\s+/g, " ").trim()}`));
});

// ---------------------------------------------------------------- redirects

test("old routes redirect; current routes do not", () => {
  assert.equal(redirectFor("/live/abc-123"), "/analyses/abc-123");
  for (const p of ["/corpus", "/findings", "/trust", "/method", "/papers/x", "/papers/arxiv%3A1706.03762", "/papers", "/trust/"]) {
    assert.equal(redirectFor(p), "/", p);
  }
  for (const p of ["/", "/analyses/abc", "/nope"]) assert.equal(redirectFor(p), null, p);
});
