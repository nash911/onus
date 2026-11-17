import assert from "node:assert/strict";
import { test } from "node:test";
import { Tracker, newSessionId, optedOut, referrerHost, screenBucket, type Transport } from "../src/lib/track.ts";

function capture(): Transport & { bodies: { body: string; final: boolean }[] } {
  const bodies: { body: string; final: boolean }[] = [];
  return { bodies, send: (body, final) => void bodies.push({ body, final }) };
}

test("events carry path, analysis id and cleaned props, and flush in batches", () => {
  const t = capture();
  let path = "/";
  let clock = 1000;
  const tr = new Tracker({ enabled: true, session: "sess-0001", transport: t, path: () => path, maxBatch: 2, now: () => clock });
  tr.track("page_view", { route: "/", referrer: undefined });
  path = "/analyses/abc";
  tr.setAnalysis("abc");
  tr.track("claim_open", { claim_id: "c1", verdict: "untraced", is_main: true, via: "x".repeat(300) });
  tr.track("export", { format: "json" });
  clock = 4000;
  assert.equal(tr.pending(), 3);
  assert.equal(tr.flush(true), 3);
  assert.equal(t.bodies.length, 2);
  const first = JSON.parse(t.bodies[0].body);
  assert.equal(first.session, "sess-0001");
  assert.deepEqual(first.events[0], { type: "page_view", path: "/", props: { route: "/" }, ago: 3000 });
  assert.equal(first.events[1].analysis_id, "abc");
  assert.equal(first.events[1].props.via.length, 120);
  assert.equal(t.bodies[1].final, true);
  assert.equal(tr.pending(), 0);
});

test("a disabled tracker sends nothing, and a failing transport never throws", () => {
  const t = capture();
  const off = new Tracker({ enabled: false, session: "sess-0001", transport: t });
  off.track("page_view");
  assert.equal(off.flush(), 0);
  assert.equal(t.bodies.length, 0);
  const bad = new Tracker({ enabled: true, session: "sess-0001", transport: { send: () => { throw new Error("x"); } } });
  bad.track("page_view");
  assert.doesNotThrow(() => bad.flush());
  assert.equal(bad.pending(), 0);
});

test("the queue is capped", () => {
  const tr = new Tracker({ enabled: true, session: "sess-0001", transport: capture(), maxQueue: 3 });
  for (let i = 0; i < 10; i++) tr.track("heartbeat", { seconds: 15 });
  assert.equal(tr.pending(), 3);
});

test("privacy and helpers", () => {
  assert.equal(optedOut({ doNotTrack: "1" }), true);
  assert.equal(optedOut({ globalPrivacyControl: true }), true);
  assert.equal(optedOut({ doNotTrack: null }), false);
  assert.equal(optedOut(undefined), true);
  assert.match(newSessionId(() => 0.5), /^[0-9a-f-]{36}$/);
  assert.equal(screenBucket(390), "phone");
  assert.equal(screenBucket(1920), "desktop");
  assert.equal(referrerHost("https://news.ycombinator.com/item?id=1", "www.onuscheck.com"), "news.ycombinator.com");
  assert.equal(referrerHost("https://www.onuscheck.com/", "www.onuscheck.com"), undefined);
  assert.equal(referrerHost("not a url", "x"), undefined);
});
