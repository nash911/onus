// Unit tests for the frontend's pure helpers. Run with `npm test` (Node's built-in runner; Node >= 22.18
// strips the TypeScript types itself, so no extra dependency is needed).
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { factLabel, missingProgram, rerunView } from "../src/lib/codecheck.ts";
import { codeSourceText, codeStageLine, codeStagePending, liveModeLabel } from "../src/lib/codeStage.ts";
import { LIVE_OFF_TEXT, startErrorText } from "../src/lib/startErrors.ts";
import { isOlderVersion } from "../src/lib/versions.ts";

// ---------------------------------------------------------------- item 2: older versions

test("older-version badge only when both fingerprints are known and differ", () => {
  assert.equal(isOlderVersion("b19c7c9dcdcd", "9ad544b97609"), true);
  assert.equal(isOlderVersion("9ad544b97609", "9ad544b97609"), false);
  assert.equal(isOlderVersion(undefined, "9ad544b97609"), false);
  assert.equal(isOlderVersion("b19c7c9dcdcd", undefined), false); // meta not loaded yet: no badge
  assert.equal(isOlderVersion(null, null), false);
});

// ---------------------------------------------------------------- item 3: code-check outcomes

test("fails_as_shipped shows plain words and the error line", () => {
  const v = rerunView({
    status: "fails_as_shipped",
    run_status: "fails_as_shipped",
    reason: "python main.py -> ModuleNotFoundError: No module named 'foo'",
    as_shipped: { outcome: "fails_as_shipped", cmd: "python main.py", rc: 1, error_line: "ModuleNotFoundError: No module named 'foo'" },
    commands: ["$ python main.py   # exit 1, 3.2 s"],
    minutes: 4.25,
  });
  assert.equal(v.label, "Fails as submitted");
  assert.equal(v.tone, "bad");
  assert.equal(v.errorLine, "ModuleNotFoundError: No module named 'foo'");
  assert.deepEqual(v.commands, ["$ python main.py   # exit 1, 3.2 s"]);
  assert.equal(v.minutes, 4.25);
});

test("error line falls back to the command log", () => {
  const v = rerunView({ status: "missing_data", command_log: [{ cmd: "python a.py", rc: 1, error_line: "FileNotFoundError: /data/x.csv" }] });
  assert.equal(v.label, "Data not included");
  assert.equal(v.errorLine, "FileNotFoundError: /data/x.csv");
});

test("install_failed and failed (agent ran out of steps)", () => {
  assert.equal(rerunView({ status: "install_failed", reason: "install failed: ERROR: No matching distribution" }).label, "Dependencies would not install");
  const f = rerunView({ status: "failed", reason: "max_steps" });
  assert.equal(f.label, "Re-run ran out of steps");
  assert.match(f.explain, /step or time limit/);
  assert.equal(f.errorLine, null);
});

test("a re-run that only worked after modifications carries its diff", () => {
  const mods = [{ path: "train.py", change: "removed CUDA call", diff: "--- a/train.py\n+++ b/train.py\n-x.cuda()\n+x" }];
  for (const x of [
    { status: "consistent_modified", run_status: "reproduced", modified: true, modifications: mods },
    { status: "reproduced", modified: true, modifications: mods }, // raw status from an older bundle
  ]) {
    const v = rerunView(x);
    assert.equal(v.label, "Matched only after the code was changed");
    assert.equal(v.tone, "caution");
    assert.equal(v.modifications.length, 1);
    assert.equal(v.modifications[0]!.diff, mods[0]!.diff);
  }
});

test("unknown statuses still render with the raw value", () => {
  const v = rerunView({ status: "quantum_flux", reason: "something new" });
  assert.equal(v.known, false);
  assert.equal(v.label, "quantum_flux");
  assert.equal(v.reason, "something new");
  assert.equal(rerunView({}).label, "unknown");
});

test("command objects are rendered as text, never [object Object]", () => {
  const v = rerunView({ status: "runs", commands: [{ cmd: "python run.py", rc: 0 }, 42, null] });
  assert.deepEqual(v.commands, ["$ python run.py   # exit 0"]);
});

test("static fact labels", () => {
  assert.equal(factLabel("contradicts"), "Code contradicts the paper");
  assert.equal(factLabel("mismatch"), "Paper and code disagree");
  assert.equal(factLabel("brand_new"), "brand_new");
  assert.equal(factLabel(undefined), "Unknown");
});

// ---------------------------------------------------------------- item 5: live analysis errors

test("429 limit messages are shown as readable text", () => {
  for (const detail of [
    "You can start at most 5 live analyses per hour. Please try again later.",
    "The daily limit of 40 live analyses has been reached. Please try again tomorrow (UTC).",
    "Today's budget for live analyses is used up. Please try again tomorrow (UTC), or browse the analysed corpus.",
  ]) {
    assert.deepEqual(startErrorText(429, detail), { title: "Limit reached", body: detail });
  }
  // no detail in the body: still readable, never "HTTP 429"
  assert.doesNotMatch(startErrorText(429, "Request failed (HTTP 429).").body, /HTTP/);
});

test("live switched off (503)", () => {
  assert.equal(startErrorText(503, null).title, "Live analysis is off");
  assert.equal(startErrorText(503, null).body, LIVE_OFF_TEXT);
  assert.equal(startErrorText(413, "The PDF is larger than 20 MB.").body, "The PDF is larger than 20 MB.");
});

// ---------------------------------------------------------------- Phase 11b: live code checks

test("code source wording", () => {
  assert.equal(codeSourceText({ status: "done", source: { kind: "upload" } }), "Code uploaded with the submission.");
  assert.equal(
    codeSourceText({
      status: "done",
      source: { kind: "repo", url: "https://github.com/tensorflow/tensor2tensor", fetched_at: "2026-11-16" },
    }),
    "Code from https://github.com/tensorflow/tensor2tensor, default branch as of 2026-11-16; this may differ from the code the paper was submitted with.",
  );
  assert.equal(codeSourceText({ status: "no_code", source: { kind: "none" } }), null);
  assert.equal(codeSourceText(null), null);
});

test("code stage status lines and polling", () => {
  assert.equal(codeStagePending({ status: "running" }), true);
  assert.equal(codeStagePending({ status: "queued" }), true);
  assert.equal(codeStagePending({ status: "done" }), false);
  assert.equal(codeStagePending(null), false);
  assert.match(codeStageLine({ status: "running" })!, /^Code checks running…/);
  assert.equal(codeStageLine({ status: "failed", message: "The repository was not found." }), "The repository was not found.");
  assert.equal(codeStageLine({ status: "no_code" }), null); // the existing "No code was submitted" wording stays
  assert.equal(codeStageLine({ status: "done" }), null);
  assert.equal(liveModeLabel({ status: "done" }), "Live analysis, 1 run, with code checks");
  assert.equal(liveModeLabel(undefined), "Live analysis, 1 run, no code checks");
  assert.equal(rerunView({ status: "skipped", reason: "Skipped: the daily limit" }).label, "Re-run skipped");
});

// ---------------------------------------------------------------- Phase 11c: a system program was missing

test("a missing system program is not shown as missing data (fixture 2611.10459)", () => {
  // Production re-run: every simulation ran; only the final figure failed because latex was not installed.
  const fx = JSON.parse(readFileSync(new URL("../../../tests/fixtures/live_2611.10459.json", import.meta.url), "utf8"));
  const x = fx.code.reruns[0];
  assert.equal(x.status, "missing_data"); // the stored status stays as it is
  const v = rerunView(x);
  assert.equal(v.label, "Ran, but a system program the code calls was not installed: latex");
  assert.equal(v.missingProgram, "latex");
  assert.doesNotMatch(v.explain, /needs data/);
  assert.equal(v.errorLine, "FileNotFoundError: [Errno 2] No such file or directory: 'latex'");
  assert.match(v.note!, /Matplotlib could not find LaTeX/);
  assert.equal(v.status, "missing_data");
});

test("a missing file is still missing data", () => {
  assert.equal(missingProgram("FileNotFoundError: [Errno 2] No such file or directory: 'data/train.csv'"), null);
  assert.equal(missingProgram("FileNotFoundError: [Errno 2] No such file or directory: 'weights.pt'"), null);
  assert.equal(missingProgram("FileNotFoundError: [Errno 2] No such file or directory: '/usr/bin/ffmpeg'"), null);
  assert.equal(missingProgram('FileNotFoundError: [Errno 2] No such file or directory: "ffmpeg"'), "ffmpeg");
  assert.equal(missingProgram(null), null);
  const v = rerunView({ status: "missing_data", as_shipped: { error_line: "FileNotFoundError: [Errno 2] No such file or directory: 'data/x.csv'" }, agent_note: "n" });
  assert.equal(v.label, "Data not included");
  assert.equal(v.note, null);
});
